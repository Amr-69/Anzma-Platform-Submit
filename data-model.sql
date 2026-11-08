-- =====================================================================
-- Anzma AI Insight - unified schema, migration 001
-- Target: PostgreSQL 16.  Status: DRAFT for Day 1 review (see docs/DATA_MODEL.md)
-- Principles: telemetry/events are append-only facts; current condition lives
-- in asset_state; alerts and incidents have separate lifecycles; evidence keeps
-- hard foreign keys to its source rows; nothing cascades (ON DELETE RESTRICT).
-- =====================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS eval;   -- test-harness ground truth; the engine must never read it

-- ---------------------------------------------------------------- helpers
CREATE FUNCTION prevent_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
END $$;

-- ---------------------------------------------------------------- 1. Building
CREATE TABLE buildings (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code             TEXT NOT NULL UNIQUE,
  name             TEXT NOT NULL,
  address          TEXT,
  timezone         TEXT NOT NULL,                    -- IANA name, validated by trigger below
  work_start_local TIME NOT NULL DEFAULT '08:00',    -- replaces hard-coded 08-18 / UTC+2
  work_end_local   TIME NOT NULL DEFAULT '18:00',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (work_start_local < work_end_local)
);

CREATE FUNCTION assert_valid_timezone() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = NEW.timezone) THEN
    RAISE EXCEPTION 'unknown IANA time zone: %', NEW.timezone;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_buildings_tz BEFORE INSERT OR UPDATE OF timezone ON buildings
  FOR EACH ROW EXECUTE FUNCTION assert_valid_timezone();

-- ---------------------------------------------------------------- 2. Location
CREATE TABLE locations (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  building_id  BIGINT NOT NULL REFERENCES buildings(id) ON DELETE RESTRICT,
  floor        INT  NOT NULL,
  zone         TEXT NOT NULL,
  zone_type    TEXT NOT NULL DEFAULT 'GENERAL',      -- e.g. SERVER_ROOM, LOBBY (rules use this, not the zone name)
  is_sensitive BOOLEAN NOT NULL DEFAULT false,       -- stricter thresholds / after-hours rules
  UNIQUE (building_id, floor, zone),
  UNIQUE (id, building_id)                           -- target of the composite FK from assets
);

-- ---------------------------------------------------------------- 3. System (+ type catalogues)
CREATE TABLE systems (                               -- a row, not an enum: "future building systems" = INSERT
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code        TEXT NOT NULL UNIQUE CHECK (code = upper(code) AND code ~ '^[A-Z_]+$'),
  name        TEXT NOT NULL,
  legacy_code TEXT UNIQUE                            -- NETWORK / ACCESS used by the old repo
);

CREATE TABLE asset_types (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  system_id           BIGINT NOT NULL REFERENCES systems(id) ON DELETE RESTRICT,
  code                TEXT NOT NULL UNIQUE CHECK (code = upper(code) AND code ~ '^[A-Z_]+$'),
  name                TEXT NOT NULL,
  expected_interval_s INT,                           -- NULL = event-driven, no liveness expectation
  stale_after_s       INT,
  UNIQUE (id, system_id),
  CHECK ((expected_interval_s IS NULL) = (stale_after_s IS NULL)),
  CHECK (expected_interval_s IS NULL OR (expected_interval_s > 0 AND stale_after_s >= expected_interval_s))
);

CREATE TABLE metric_types (                          -- numeric/boolean samples -> telemetry
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code       TEXT NOT NULL UNIQUE CHECK (code = lower(code) AND code ~ '^[a-z0-9_]+$'),
  unit       TEXT NOT NULL,
  value_kind TEXT NOT NULL DEFAULT 'NUMERIC' CHECK (value_kind IN ('NUMERIC','BOOLEAN')),
  min_value  DOUBLE PRECISION NOT NULL,              -- plausibility range, enforced on insert
  max_value  DOUBLE PRECISION NOT NULL,
  CHECK (min_value < max_value)
);

CREATE TABLE metric_type_asset_types (
  metric_type_id BIGINT NOT NULL REFERENCES metric_types(id) ON DELETE RESTRICT,
  asset_type_id  BIGINT NOT NULL REFERENCES asset_types(id)  ON DELETE RESTRICT,
  PRIMARY KEY (metric_type_id, asset_type_id)
);

CREATE TABLE event_types (                           -- discrete occurrences -> events
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code                   TEXT NOT NULL UNIQUE CHECK (code = upper(code) AND code ~ '^[A-Z_]+$'),
  kind                   TEXT NOT NULL CHECK (kind IN ('ALARM','FAULT','OCCURRENCE','RECOVERY','LIVENESS')),
  is_fault               BOOLEAN NOT NULL,           -- never assume an event is a fault
  resolves_event_type_id BIGINT REFERENCES event_types(id) ON DELETE RESTRICT,
  description            TEXT,
  CHECK (is_fault = (kind IN ('ALARM','FAULT'))),
  CHECK ((kind = 'RECOVERY') = (resolves_event_type_id IS NOT NULL))
);

CREATE TABLE event_type_asset_types (                -- compatibility matrix (what may be recorded on what)
  event_type_id BIGINT NOT NULL REFERENCES event_types(id) ON DELETE RESTRICT,
  asset_type_id BIGINT NOT NULL REFERENCES asset_types(id) ON DELETE RESTRICT,
  PRIMARY KEY (event_type_id, asset_type_id)
);

-- ---------------------------------------------------------------- 4. Asset
CREATE TABLE assets (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  asset_code       TEXT NOT NULL UNIQUE,
  name             TEXT NOT NULL,
  building_id      BIGINT NOT NULL,
  location_id      BIGINT NOT NULL,
  system_id        BIGINT NOT NULL,
  asset_type_id    BIGINT NOT NULL,
  model            TEXT,
  lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE'
                   CHECK (lifecycle_status IN ('ACTIVE','INACTIVE','MAINTENANCE','DECOMMISSIONED')),
  installed_at     DATE,
  metadata         JSONB NOT NULL DEFAULT '{}',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (location_id, building_id)  REFERENCES locations(id, building_id)   ON DELETE RESTRICT,
  FOREIGN KEY (asset_type_id, system_id)  REFERENCES asset_types(id, system_id)   ON DELETE RESTRICT,
  UNIQUE (id, asset_type_id)                         -- target of the composite FKs from telemetry/events
);
CREATE INDEX idx_assets_system   ON assets (system_id);
CREATE INDEX idx_assets_location ON assets (location_id);

-- ---------------------------------------------------------------- 5. Telemetry (append-only samples)
CREATE TABLE telemetry (
  asset_id       BIGINT NOT NULL,
  asset_type_id  BIGINT NOT NULL,
  metric_type_id BIGINT NOT NULL,
  source_ts      TIMESTAMPTZ NOT NULL,               -- when the device measured it
  received_at    TIMESTAMPTZ NOT NULL DEFAULT now(), -- when the platform stored it
  value          DOUBLE PRECISION NOT NULL CHECK (value > '-Infinity' AND value < 'Infinity'),  -- rejects NaN/inf
  source         TEXT NOT NULL DEFAULT 'api',        -- e.g. api, simulator, legacy-migration
  PRIMARY KEY (asset_id, metric_type_id, source_ts), -- natural key = idempotent ingestion; partition-ready
  FOREIGN KEY (asset_id, asset_type_id)           REFERENCES assets(id, asset_type_id)                 ON DELETE RESTRICT,
  FOREIGN KEY (metric_type_id, asset_type_id)     REFERENCES metric_type_asset_types(metric_type_id, asset_type_id) ON DELETE RESTRICT,
  FOREIGN KEY (metric_type_id)                    REFERENCES metric_types(id)                          ON DELETE RESTRICT,
  CHECK (source_ts <= received_at + interval '5 minutes')
);
CREATE INDEX idx_telemetry_ts ON telemetry (source_ts);

CREATE FUNCTION telemetry_validate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE m metric_types%ROWTYPE;
BEGIN
  SELECT * INTO m FROM metric_types WHERE id = NEW.metric_type_id;
  IF NEW.value < m.min_value OR NEW.value > m.max_value THEN
    RAISE EXCEPTION 'value % outside plausible range [%, %] for metric %', NEW.value, m.min_value, m.max_value, m.code;
  END IF;
  IF m.value_kind = 'BOOLEAN' AND NEW.value NOT IN (0, 1) THEN
    RAISE EXCEPTION 'boolean metric % accepts only 0 or 1', m.code;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_telemetry_validate BEFORE INSERT ON telemetry FOR EACH ROW EXECUTE FUNCTION telemetry_validate();
CREATE TRIGGER trg_telemetry_append_only BEFORE UPDATE OR DELETE ON telemetry FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

-- ---------------------------------------------------------------- 6. Event (append-only occurrences)
CREATE TABLE events (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  asset_id        BIGINT NOT NULL,
  asset_type_id   BIGINT NOT NULL,
  event_type_id   BIGINT NOT NULL,
  source_ts       TIMESTAMPTZ NOT NULL,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  reported_severity TEXT NOT NULL DEFAULT 'INFO' CHECK (reported_severity IN ('INFO','WARNING','CRITICAL')), -- as sent by the device; NOT the alert severity
  value           DOUBLE PRECISION CHECK (value > '-Infinity' AND value < 'Infinity'),
  unit            TEXT,
  details         JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,              -- sha256(asset_code|event_type|source_ts|canonical payload), set by ingestion
  source          TEXT NOT NULL DEFAULT 'api',
  UNIQUE (id, asset_id),
  FOREIGN KEY (asset_id, asset_type_id)       REFERENCES assets(id, asset_type_id)                         ON DELETE RESTRICT,
  FOREIGN KEY (event_type_id, asset_type_id)  REFERENCES event_type_asset_types(event_type_id, asset_type_id) ON DELETE RESTRICT,
  FOREIGN KEY (event_type_id)                 REFERENCES event_types(id)                                   ON DELETE RESTRICT,
  CHECK (source_ts <= received_at + interval '5 minutes'),
  CHECK (jsonb_typeof(details) = 'object')
);
CREATE INDEX idx_events_asset_ts ON events (asset_id, source_ts DESC);
CREATE INDEX idx_events_type_ts  ON events (event_type_id, source_ts DESC);
CREATE INDEX idx_events_ts       ON events (source_ts);
CREATE TRIGGER trg_events_append_only BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

-- ---------------------------------------------------------------- asset_state (CURRENT condition, derived)
CREATE TABLE asset_state (
  asset_id           BIGINT PRIMARY KEY REFERENCES assets(id) ON DELETE RESTRICT,
  operational_status TEXT NOT NULL CHECK (operational_status IN ('NORMAL','WARNING','CRITICAL','UNKNOWN')),
  freshness          TEXT NOT NULL CHECK (freshness IN ('FRESH','STALE','NO_DATA','NOT_APPLICABLE')),
  status_reason      TEXT NOT NULL,
  status_since       TIMESTAMPTZ NOT NULL,
  last_seen_at       TIMESTAMPTZ,                    -- latest telemetry/event/heartbeat of ANY kind
  open_fault_event_id BIGINT REFERENCES events(id) ON DELETE RESTRICT,  -- opener with no recovery yet
  computed_as_of     TIMESTAMPTZ NOT NULL,           -- the clock used (wall clock, or an explicit replay time)
  computed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (freshness <> 'NO_DATA' OR last_seen_at IS NULL),
  CHECK (freshness IN ('FRESH','NOT_APPLICABLE') OR operational_status <> 'NORMAL')  -- silence is never "healthy"
);

-- ---------------------------------------------------------------- alert rules + playbook (approved engineering logic)
CREATE TABLE alert_rules (
  rule_id                TEXT PRIMARY KEY,
  title                  TEXT NOT NULL,
  detection_method       TEXT NOT NULL CHECK (detection_method IN ('THRESHOLD','ANOMALY','COMPOUND')),
  playbook_id            TEXT NOT NULL,
  resolution_mode        TEXT NOT NULL CHECK (resolution_mode IN ('RECOVERY_EVENT','CONDITION_CLEARED','MANUAL')),
  recovery_event_type_id BIGINT REFERENCES event_types(id) ON DELETE RESTRICT,
  version                INT NOT NULL DEFAULT 1,
  is_enabled             BOOLEAN NOT NULL DEFAULT true,
  config                 JSONB NOT NULL DEFAULT '{}',   -- thresholds (moved out of alert_config.py in Day 2)
  CHECK ((resolution_mode = 'RECOVERY_EVENT') = (recovery_event_type_id IS NOT NULL))
);

CREATE TABLE playbook_entries (
  entry_id        TEXT NOT NULL,
  version         INT  NOT NULL DEFAULT 1,
  playbook_id     TEXT NOT NULL,
  entry_kind      TEXT NOT NULL CHECK (entry_kind IN ('CAUSE','ACTION')),
  priority        INT,
  text            TEXT NOT NULL,
  condition       JSONB NOT NULL DEFAULT '[]',          -- [{"fact":..,"op":"=|>|>=|<|<=","value":..}]
  approval_status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (approval_status IN ('DRAFT','APPROVED','RETIRED')),
  approved_by     TEXT,
  approved_at     TIMESTAMPTZ,
  PRIMARY KEY (entry_id, version),
  UNIQUE (entry_id, version, approval_status),          -- target of the recommendations FK
  CHECK (approval_status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK (entry_kind <> 'ACTION' OR priority IS NOT NULL)
);

-- ---------------------------------------------------------------- 7. Alert
CREATE TABLE alerts (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id           TEXT   NOT NULL REFERENCES alert_rules(rule_id) ON DELETE RESTRICT,
  rule_version      INT    NOT NULL,
  asset_id          BIGINT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
  severity          TEXT   NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
  title             TEXT   NOT NULL,
  summary           TEXT   NOT NULL,
  first_ts          TIMESTAMPTZ NOT NULL,
  last_ts           TIMESTAMPTZ NOT NULL,
  detected_ts       TIMESTAMPTZ NOT NULL,
  dedup_key         TEXT   NOT NULL UNIQUE,              -- rule_id|asset_id|first_ts: re-running the engine upserts, never deletes
  facts             JSONB  NOT NULL DEFAULT '{}',        -- measured facts used by playbook conditions
  thresholds        JSONB  NOT NULL DEFAULT '{}',
  status            TEXT   NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED','AUTO_RESOLVED')),
  status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at       TIMESTAMPTZ,
  resolution_note   TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (last_ts >= first_ts AND detected_ts >= first_ts),
  CHECK ((status IN ('RESOLVED','AUTO_RESOLVED')) = (resolved_at IS NOT NULL))
);
CREATE INDEX idx_alerts_asset_ts ON alerts (asset_id, first_ts DESC);
CREATE INDEX idx_alerts_open     ON alerts (severity, first_ts DESC) WHERE status IN ('OPEN','ACKNOWLEDGED');

-- ---------------------------------------------------------------- 8. Incident (+ link)
CREATE TABLE incidents (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  building_id      BIGINT NOT NULL REFERENCES buildings(id) ON DELETE RESTRICT,
  primary_asset_id BIGINT REFERENCES assets(id) ON DELETE RESTRICT,
  title            TEXT NOT NULL,
  severity         TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
  status           TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACKNOWLEDGED','INVESTIGATING','RESOLVED')),
  correlation_key  TEXT,
  opened_at        TIMESTAMPTZ NOT NULL,
  acknowledged_at  TIMESTAMPTZ,
  resolved_at      TIMESTAMPTZ,
  resolution_note  TEXT,
  CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL)),
  CHECK (resolved_at IS NULL OR resolved_at >= opened_at)
);
CREATE UNIQUE INDEX uq_incidents_open_correlation ON incidents (correlation_key)
  WHERE correlation_key IS NOT NULL AND status <> 'RESOLVED';     -- one open incident per correlation key (deduplication)
CREATE INDEX idx_incidents_open ON incidents (severity, opened_at DESC) WHERE status <> 'RESOLVED';

CREATE TABLE incident_alerts (
  incident_id BIGINT NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
  alert_id    BIGINT NOT NULL UNIQUE REFERENCES alerts(id) ON DELETE RESTRICT,  -- an alert belongs to at most one incident
  link_reason TEXT NOT NULL CHECK (link_reason IN ('SAME_ASSET','SAME_LOCATION','COMPOUND_RULE','MANUAL')),
  linked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (incident_id, alert_id)
);

-- ---------------------------------------------------------------- 9. Evidence (hard FKs to source rows)
CREATE TABLE evidence (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_id            BIGINT NOT NULL REFERENCES alerts(id) ON DELETE RESTRICT,
  ref_label           TEXT   NOT NULL CHECK (ref_label ~ '^E[0-9]+$'),    -- E1, E2 ... cited by explanations
  role                TEXT   NOT NULL CHECK (role IN ('TRIGGER','CONTEXT','RECOVERY','BASELINE')),
  source_event_id     BIGINT REFERENCES events(id) ON DELETE RESTRICT,
  src_tel_asset_id    BIGINT,
  src_tel_metric_id   BIGINT,
  src_tel_ts          TIMESTAMPTZ,
  snapshot            JSONB  NOT NULL,                                    -- verbatim copy of the source row at collection time
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (alert_id, ref_label),
  FOREIGN KEY (src_tel_asset_id, src_tel_metric_id, src_tel_ts)
    REFERENCES telemetry(asset_id, metric_type_id, source_ts) ON DELETE RESTRICT,
  CHECK (num_nonnulls(source_event_id, src_tel_asset_id) = 1),            -- exactly one source kind
  CHECK ((src_tel_asset_id IS NULL) = (src_tel_metric_id IS NULL) AND (src_tel_asset_id IS NULL) = (src_tel_ts IS NULL))
);
CREATE INDEX idx_evidence_event ON evidence (source_event_id);

-- ---------------------------------------------------------------- 10. Recommendation
CREATE TABLE recommendations (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_id              BIGINT REFERENCES alerts(id)    ON DELETE RESTRICT,
  incident_id           BIGINT REFERENCES incidents(id) ON DELETE RESTRICT,
  entry_id              TEXT NOT NULL,
  entry_version         INT  NOT NULL,
  logic_approval_status TEXT NOT NULL,                  -- copied from the playbook entry and kept honest by the FK below
  action_text           TEXT NOT NULL,                  -- wording exactly as shown to the user
  priority              INT  NOT NULL,
  satisfied_facts       JSONB NOT NULL,                 -- the measured facts that made the condition true
  requires_human_confirmation BOOLEAN NOT NULL DEFAULT true,
  status                TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED','IN_PROGRESS','DONE','DISMISSED')),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (entry_id, entry_version, logic_approval_status)
    REFERENCES playbook_entries(entry_id, version, approval_status) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CHECK (alert_id IS NOT NULL OR incident_id IS NOT NULL)
);
CREATE TABLE recommendation_evidence (
  recommendation_id BIGINT NOT NULL REFERENCES recommendations(id) ON DELETE RESTRICT,
  evidence_id       BIGINT NOT NULL REFERENCES evidence(id)        ON DELETE RESTRICT,
  PRIMARY KEY (recommendation_id, evidence_id)
);

-- ---------------------------------------------------------------- AI explanations (validation status is stored, never assumed)
CREATE TABLE explanations (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_id          BIGINT REFERENCES alerts(id)    ON DELETE RESTRICT,
  incident_id       BIGINT REFERENCES incidents(id) ON DELETE RESTRICT,
  origin            TEXT NOT NULL CHECK (origin IN ('LLM','FALLBACK_FACTS','RULES')),
  model             TEXT,
  text              TEXT NOT NULL,
  validation_status TEXT NOT NULL DEFAULT 'AI_GENERATED'
                    CHECK (validation_status IN ('AI_GENERATED','EVIDENCE_SUPPORTED','VALIDATED','PARTIALLY_SUPPORTED','UNSUPPORTED')),
  checks_run        TEXT[] NOT NULL DEFAULT '{}',        -- e.g. {numbers,times,citations,names,semantic}
  violations        JSONB NOT NULL DEFAULT '[]',
  attempts          INT NOT NULL DEFAULT 1,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (alert_id IS NOT NULL OR incident_id IS NOT NULL),
  CHECK (origin <> 'LLM' OR model IS NOT NULL),
  CHECK (validation_status <> 'VALIDATED' OR 'semantic' = ANY (checks_run))   -- "validated" requires a semantic check to have run
);

-- ---------------------------------------------------------------- test harness (never read by the engine)
CREATE TABLE eval.scenario_runs (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scenario_key   TEXT NOT NULL,
  name           TEXT NOT NULL,
  description    TEXT NOT NULL,
  asset_ids      BIGINT[] NOT NULL,
  start_ts       TIMESTAMPTZ NOT NULL,
  end_ts         TIMESTAMPTZ NOT NULL,
  expected_rules TEXT[] NOT NULL,
  CHECK (end_ts > start_ts)
);

-- ---------------------------------------------------------------- convenience views
CREATE VIEW v_assets AS
SELECT a.id AS asset_id, a.asset_code, a.name, a.lifecycle_status,
       b.code AS building, l.floor, l.zone, l.zone_type, l.is_sensitive,
       s.code AS system, t.code AS asset_type, a.model
FROM assets a
JOIN buildings b   ON b.id = a.building_id
JOIN locations l   ON l.id = a.location_id
JOIN systems s     ON s.id = a.system_id
JOIN asset_types t ON t.id = a.asset_type_id;

CREATE VIEW v_events AS
SELECT e.id AS event_id, e.source_ts, e.received_at, v.asset_code, v.system, v.asset_type, v.building, v.zone,
       et.code AS event_type, et.kind, et.is_fault, e.reported_severity, e.value, e.unit, e.details, e.source
FROM events e
JOIN event_types et ON et.id = e.event_type_id
JOIN v_assets v     ON v.asset_id = e.asset_id;

COMMIT;

## Entity-relationship diagram

![Unified ERD](docs/ERD-new.png)