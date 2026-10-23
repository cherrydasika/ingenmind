-- IngenMind analytics export (R57): views over the exported Parquet files and
-- example queries. Open it from the export folder:
--
--     duckdb -init explore.sql
--
-- Each view is one file. A file missing from this export (its table didn't
-- exist yet) makes that view fail, and the rest still work. user_ref and
-- session_ref are pseudonymous. Question and answer text are present only in
-- an export made with --include-text.

CREATE OR REPLACE VIEW questions AS SELECT * FROM 'questions.parquet';
CREATE OR REPLACE VIEW citations AS SELECT * FROM 'citations.parquet';
CREATE OR REPLACE VIEW runs AS SELECT * FROM 'runs.parquet';
CREATE OR REPLACE VIEW run_agents AS SELECT * FROM 'run_agents.parquet';
CREATE OR REPLACE VIEW eval_runs AS SELECT * FROM 'eval_runs.parquet';
CREATE OR REPLACE VIEW eval_results AS SELECT * FROM 'eval_results.parquet';
CREATE OR REPLACE VIEW ingestion_jobs AS SELECT * FROM 'ingestion_jobs.parquet';
CREATE OR REPLACE VIEW pages AS SELECT * FROM 'pages.parquet';
CREATE OR REPLACE VIEW knowledge_gaps AS SELECT * FROM 'knowledge_gaps.parquet';
CREATE OR REPLACE VIEW feedback AS SELECT * FROM 'feedback.parquet';

-- Answer pass rate per week (users' questions whose answer was checked)
SELECT date_trunc('week', CAST(asked_at AS TIMESTAMP)) AS week, count(*) AS questions,
       count(passed) AS checked, round(avg(CAST(passed AS INTEGER)), 3) AS pass_rate
FROM questions GROUP BY 1 ORDER BY 1;

-- Most cited sources
SELECT url, count(DISTINCT turn_id) AS answers, sum(citations) AS citations
FROM citations GROUP BY 1 ORDER BY answers DESC, citations DESC LIMIT 20;

-- Slowest questions (an export made with --include-text also has their text: add the question column)
SELECT turn_id, asked_at, round(seconds, 1) AS seconds, channel, passed
FROM questions WHERE seconds IS NOT NULL ORDER BY seconds DESC LIMIT 20;

-- Tokens per day and model, from the run records
SELECT CAST(started_at AS DATE) AS day, model, count(*) AS runs, sum(input_tokens + output_tokens) AS tokens,
       round(quantile_cont(seconds, 0.95), 1) AS p95_seconds
FROM runs GROUP BY 1, 2 ORDER BY 1, 2;

-- Evaluation pass rate per set and flow version
SELECT set_name, flow_id, version, count(*) AS runs, round(avg(pass_rate), 3) AS pass_rate
FROM eval_runs WHERE status = 'done' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
