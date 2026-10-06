"""Outcome coverage diagnostics. Every column here is audit-only, never a feature.

The eventual-delisting grouping uses the supplied snapshot (including future
events). It must not select historical features or trading universes.
"""
from .catalog import sql_literal as q


def create_labels(con, horizon, eval_end):
    """Keep expected session horizons distinct from observed/mature outcomes."""
    con.execute(f"""CREATE TEMP TABLE labels AS
      WITH windows AS (
        SELECT permno,date,session_index,
          count(*) OVER w AS observed_sessions,
          count(ret_total_for_signal) OVER w AS valid_sessions,
          count(*) FILTER(WHERE delist_flag='Y') OVER w AS terminal_sessions,
          count(*) FILTER(WHERE ret_total IS NULL) OVER w AS missing_return_sessions,
          count(*) FILTER(WHERE ret_total IS NOT NULL AND ret_total_for_signal IS NULL) OVER w AS incomplete_return_sessions,
          max(return_available_at) OVER w AS observed_available_at,
          count(*) FILTER(WHERE ret_total_for_signal=-1) OVER w AS zeros,
          sum(CASE WHEN ret_total_for_signal > -1 THEN ln(1+ret_total_for_signal) ELSE 0 END) OVER w AS log_product
        FROM run_panel
        WINDOW w AS (PARTITION BY permno ORDER BY session_index RANGE BETWEEN 1 FOLLOWING AND {horizon} FOLLOWING)
      ), classified AS (
        SELECT w.*,c.date AS label_end_date,c.close_at AS label_end_time,
          c.available_at AS expected_available_at,
          c.date>{q(eval_end)}::DATE AS period_end_truncated,
          CASE WHEN terminal_sessions>0 THEN 'TERMINAL_OUTCOME'
               WHEN c.date>{q(eval_end)}::DATE THEN 'PERIOD_END_TRUNCATED'
               WHEN observed_sessions<{horizon} THEN 'SESSION_GAP'
               WHEN missing_return_sessions>0 THEN 'MISSING_RETURN'
               WHEN valid_sessions<{horizon} THEN 'INCOMPLETE_RETURN'
               ELSE 'OBSERVED_APPROXIMATE_AVAILABILITY' END AS label_status
        FROM windows w JOIN calendar c ON c.session_index=w.session_index+{horizon}
      ) SELECT permno,date AS decision_date,label_end_date,label_end_time,expected_available_at,
          CASE WHEN label_status='OBSERVED_APPROXIMATE_AVAILABILITY' THEN observed_available_at END AS available_at,
          CASE WHEN label_status='OBSERVED_APPROXIMATE_AVAILABILITY'
               THEN CASE WHEN zeros>0 THEN -1.0 ELSE exp(log_product)-1 END END AS forward_vendor_return,
          label_status, observed_sessions,valid_sessions,terminal_sessions,missing_return_sessions,
          incomplete_return_sessions,period_end_truncated,
          observed_sessions<{horizon} AND NOT period_end_truncated AS has_session_gap
        FROM classified""")


def create_training_audit(con, plan):
    """Audit all feature rows; the training denominator is eligible past inputs.

    Separate expected maturity from outcome observability, including when a
    security disappears altogether. A missing same-decision observation is a
    session gap rather than an inner join silently removing the candidate.
    """
    training_range = "false" if plan.train_start is None else (
        f"f.decision_date BETWEEN {q(plan.train_start)}::DATE AND {q(plan.train_end)}::DATE")
    cutoff = f"TIMESTAMPTZ {q(plan.model_training_cutoff.isoformat())}"
    con.execute(f"""CREATE TEMP TABLE training_audit AS
      WITH candidates AS (
        SELECT f.permno,f.decision_date,year(f.decision_date) AS decision_year,
          f.primary_exchange AS exchange_observed,
          CASE WHEN {training_range} THEN 'TRAINING'
               WHEN f.decision_date BETWEEN {q(plan.eval_start)}::DATE AND {q(plan.eval_end)}::DATE
                 THEN 'EVALUATION' ELSE 'PREPARATION' END AS sample_role,
          f.eligible_for_research AS feature_eligible,
          ({training_range}) AND f.eligible_for_research AS training_candidate,
          EXISTS(SELECT 1 FROM delistings e WHERE e.permno=f.permno) AS eventual_delisting_in_snapshot,
          coalesce(l.label_status,'MISSING_DECISION_OBSERVATION') AS label_status,
          l.label_end_date,l.label_end_time,l.available_at AS label_available_at,
          l.expected_available_at,l.terminal_sessions,l.missing_return_sessions,
          l.incomplete_return_sessions,l.period_end_truncated,l.has_session_gap,
          l.expected_available_at>{cutoff} OR l.label_end_time>{cutoff} AS immature_at_cutoff,
          l.forward_vendor_return,
          CASE WHEN NOT f.eligible_for_research THEN 'FEATURE_NOT_ELIGIBLE'
               WHEN l.permno IS NULL THEN 'MISSING_DECISION_OBSERVATION'
               WHEN l.terminal_sessions>0 THEN 'TERMINAL_OUTCOME'
               WHEN l.period_end_truncated THEN 'PERIOD_END_TRUNCATED'
               WHEN l.has_session_gap THEN 'SESSION_GAP'
               WHEN l.missing_return_sessions>0 THEN 'MISSING_RETURN'
               WHEN l.label_status!='OBSERVED_APPROXIMATE_AVAILABILITY' THEN 'INCOMPLETE_RETURN'
               WHEN l.label_end_time>{cutoff} OR l.available_at>{cutoff} THEN 'LABEL_IMMATURE_CUTOFF'
               WHEN l.available_at IS NULL OR l.forward_vendor_return IS NULL THEN 'UNKNOWN_AVAILABILITY'
               ELSE 'USABLE_AT_CUTOFF' END AS outcome_disposition
        FROM features f LEFT JOIN labels l USING(permno,decision_date)
      ) SELECT * EXCLUDE(forward_vendor_return),
          training_candidate AND outcome_disposition='USABLE_AT_CUTOFF' AS training_included,
          CASE WHEN sample_role!='TRAINING' THEN 'OUTSIDE_TRAINING_WINDOW'
               WHEN outcome_disposition='USABLE_AT_CUTOFF' THEN 'INCLUDED'
               ELSE outcome_disposition END AS training_disposition,
          'AUDIT_ONLY_CONTAINS_FUTURE_OUTCOMES' AS usage
        FROM candidates""")
    # Mutually exclusive primary reasons plus separate flags avoid counting the
    # same candidate twice while retaining overlapping failure information.
    con.execute("""CREATE TEMP TABLE training_audit_summary AS
      SELECT sample_role,decision_year,exchange_observed,eventual_delisting_in_snapshot,
        count(*) AS feature_rows,
        count(*) FILTER(WHERE training_candidate) AS training_candidates,
        count(*) FILTER(WHERE training_included) AS training_included,
        count(*) FILTER(WHERE training_candidate AND NOT training_included) AS training_excluded,
        count(*) FILTER(WHERE training_candidate AND NOT training_included)::DOUBLE /
          nullif(count(*) FILTER(WHERE training_candidate),0) AS dropout_rate,
        count(*) FILTER(WHERE training_candidate AND training_disposition='TERMINAL_OUTCOME') AS terminal_excluded,
        count(*) FILTER(WHERE training_candidate AND training_disposition='LABEL_IMMATURE_CUTOFF') AS immature_excluded,
        count(*) FILTER(WHERE training_candidate AND training_disposition='PERIOD_END_TRUNCATED') AS truncated_excluded,
        count(*) FILTER(WHERE training_candidate AND training_disposition IN ('SESSION_GAP','MISSING_DECISION_OBSERVATION')) AS session_gap_excluded,
        count(*) FILTER(WHERE training_candidate AND training_disposition='MISSING_RETURN') AS missing_return_excluded,
        count(*) FILTER(WHERE training_candidate AND training_disposition IN ('INCOMPLETE_RETURN','UNKNOWN_AVAILABILITY')) AS incomplete_excluded
      FROM training_audit GROUP BY ALL""")
    candidates, included = con.execute("""SELECT count(*) FILTER(WHERE training_candidate),
        count(*) FILTER(WHERE training_included) FROM training_audit""").fetchone()
    reasons = dict(con.execute("""SELECT training_disposition,count(*) FROM training_audit
        WHERE training_candidate GROUP BY 1 ORDER BY 1""").fetchall())
    groups = con.execute("""SELECT decision_year,exchange_observed,eventual_delisting_in_snapshot,
        training_candidates,training_excluded,dropout_rate FROM training_audit_summary
        WHERE sample_role='TRAINING' AND dropout_rate>=0.10
        ORDER BY decision_year,exchange_observed,eventual_delisting_in_snapshot""").fetchall()
    warnings = [f"High training dropout: year={year}, exchange={exchange}, "
                f"eventual_delisting_in_snapshot={related}, excluded={excluded}/{n} ({rate:.1%}). "
                "Audit grouping uses hindsight; absence of leakage does not imply absence of selection bias."
                for year,exchange,related,n,excluded,rate in groups]
    return {"feature_based_candidates":candidates,"included":included,"excluded":candidates-included,
            "dropout_rate":(candidates-included)/candidates if candidates else None,
            "disposition_counts":reasons,"warning_threshold":0.10,
            "delisting_group_scope":"any event in supplied snapshot; audit only, not historical input",
            "warnings":warnings}
