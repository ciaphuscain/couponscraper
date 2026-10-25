-- CouponHunter initial schema.
-- Money: INTEGER paise. Rates: INTEGER basis points. Times: TEXT ISO-8601 UTC
-- ("YYYY-MM-DDTHH:MM:SSZ"), which sorts lexicographically == chronologically.
-- data_origin on every fact table: live | manual | imported | synthetic.

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);

CREATE TABLE products (
    id           INTEGER PRIMARY KEY,
    asin         TEXT NOT NULL UNIQUE CHECK (length(asin) = 10),
    marketplace  TEXT NOT NULL DEFAULT 'amazon.in',
    title        TEXT NOT NULL DEFAULT '',
    brand        TEXT,
    category     TEXT,
    seller       TEXT,
    variant      TEXT,
    availability TEXT,
    created_at   TEXT NOT NULL,
    data_origin  TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE watchlist (
    product_id      INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    target_price    INTEGER CHECK (target_price IS NULL OR target_price > 0),
    min_savings     INTEGER,               -- per-product override of the global threshold
    alerts_enabled  INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT NOT NULL,
    notes           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE price_observations (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price        INTEGER NOT NULL CHECK (price > 0),
    list_price   INTEGER CHECK (list_price IS NULL OR list_price > 0),
    seller       TEXT,
    shipping     INTEGER,
    in_stock     INTEGER,
    observed_at  TEXT NOT NULL,
    source       TEXT NOT NULL,
    data_origin  TEXT NOT NULL,
    UNIQUE (product_id, source, observed_at)   -- replaying an import is idempotent
);
CREATE INDEX idx_price_obs_product_time ON price_observations(product_id, observed_at);

CREATE TABLE sources (
    id                   TEXT PRIMARY KEY,
    adapter              TEXT NOT NULL,
    name                 TEXT NOT NULL,
    config               TEXT NOT NULL DEFAULT '{}',   -- JSON, never secrets
    enabled              INTEGER NOT NULL DEFAULT 1,
    terms_reviewed       INTEGER NOT NULL DEFAULT 0,   -- owner confirmed the source permits this use
    terms_note           TEXT NOT NULL DEFAULT '',
    min_interval_s       INTEGER NOT NULL DEFAULT 3600,
    stale_after_h        INTEGER NOT NULL DEFAULT 48,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_reason      TEXT,
    schema_fingerprint   TEXT,
    last_fetch_at        TEXT,
    next_fetch_at        TEXT,             -- backoff after failures / Retry-After
    last_success_at      TEXT,
    last_error           TEXT,
    last_error_at        TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE source_runs (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,      -- running | ok | partial | failed | skipped
    fetched      INTEGER NOT NULL DEFAULT 0,
    created      INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    rejected     INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX idx_source_runs_source_time ON source_runs(source_id, started_at);

CREATE TABLE offers (
    id                        INTEGER PRIMARY KEY,
    fingerprint               TEXT NOT NULL UNIQUE,  -- cross-source dedup key
    kind                      TEXT NOT NULL,
    title                     TEXT NOT NULL,
    provider                  TEXT NOT NULL,
    discount_type             TEXT NOT NULL,
    value                     INTEGER NOT NULL CHECK (value >= 0),
    max_discount              INTEGER,
    min_order                 INTEGER,
    basis                     TEXT NOT NULL,
    payment                   TEXT,                  -- JSON PaymentRequirement or NULL
    eligibility               TEXT,                  -- JSON rule tree or NULL
    new_customer_only         INTEGER NOT NULL DEFAULT 0,
    prime_only                INTEGER NOT NULL DEFAULT 0,
    account_specific          INTEGER NOT NULL DEFAULT 0,
    one_time_use              INTEGER NOT NULL DEFAULT 0,
    valid_from                TEXT,
    valid_until               TEXT,
    code                      TEXT,
    redemption_url            TEXT,
    redemption_steps          TEXT NOT NULL DEFAULT '[]',
    stacks_with               TEXT NOT NULL DEFAULT '{}',
    excludes                  TEXT NOT NULL DEFAULT '[]',
    authenticity              TEXT NOT NULL DEFAULT 'unverified',
    settlement_probability_bp INTEGER,
    source_text               TEXT NOT NULL DEFAULT '',
    -- The next three are aggregates over offer_sources, recomputed in the same
    -- transaction as every sighting (see ingest.refresh_offer_aggregates).
    first_seen_at             TEXT NOT NULL,
    last_confirmed_at         TEXT,
    withdrawn_at              TEXT,
    user_flag                 TEXT,                  -- NULL | invalid | disputed (owner override)
    data_origin               TEXT NOT NULL,
    version                   INTEGER NOT NULL DEFAULT 1  -- optimistic concurrency
);
CREATE INDEX idx_offers_valid_until ON offers(valid_until);
CREATE INDEX idx_offers_kind ON offers(kind);

-- Product-scoped offers. An offer with no rows here is not product-scoped
-- (sitewide / bank / category offer, restricted by its eligibility rule).
CREATE TABLE offer_products (
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    asin     TEXT NOT NULL,
    PRIMARY KEY (offer_id, asin)
);
CREATE INDEX idx_offer_products_asin ON offer_products(asin);

-- Provenance: every source that has listed the offer, and when.
CREATE TABLE offer_sources (
    offer_id      INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_url    TEXT,
    evidence_type TEXT NOT NULL,     -- feed_listing | official_page | user_observed | user_entered | fixture
    raw_text      TEXT NOT NULL DEFAULT '',
    authenticity  TEXT NOT NULL,     -- what this source's evidence supports
    valid_until   TEXT,              -- expiry as stated by this source
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    missing_since TEXT,              -- set when a full-snapshot run of this source no longer lists it
    PRIMARY KEY (offer_id, source_id)
);
CREATE INDEX idx_offer_sources_source ON offer_sources(source_id, last_seen_at);

CREATE TABLE redemption_attempts (
    id                   INTEGER PRIMARY KEY,
    offer_id             INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    product_id           INTEGER REFERENCES products(id) ON DELETE SET NULL,
    attempted_at         TEXT NOT NULL,
    outcome              TEXT NOT NULL CHECK (outcome IN ('applied', 'failed', 'not_shown', 'partial')),
    failure_reason       TEXT,
    observed_savings     INTEGER,
    eligibility_snapshot TEXT NOT NULL DEFAULT '{}',
    channel              TEXT NOT NULL DEFAULT 'dashboard',
    idempotency_key      TEXT UNIQUE
);
CREATE INDEX idx_redemptions_offer ON redemption_attempts(offer_id, attempted_at);

CREATE TABLE purchases (
    id               INTEGER PRIMARY KEY,
    product_id       INTEGER REFERENCES products(id) ON DELETE SET NULL,
    purchased_at     TEXT NOT NULL,
    reference_price  INTEGER NOT NULL,    -- price you would have paid with no offers
    checkout_paid    INTEGER NOT NULL,
    cash_paid        INTEGER NOT NULL,    -- differs from checkout_paid when paying with discounted balance
    offers_used      TEXT NOT NULL DEFAULT '[]',
    notes            TEXT NOT NULL DEFAULT '',
    idempotency_key  TEXT UNIQUE,
    CHECK (checkout_paid >= 0 AND cash_paid >= 0 AND reference_price > 0)
);

CREATE TABLE cashback_records (
    id            INTEGER PRIMARY KEY,
    provider      TEXT NOT NULL,
    purchase_id   INTEGER REFERENCES purchases(id) ON DELETE SET NULL,
    offer_id      INTEGER REFERENCES offers(id) ON DELETE SET NULL,
    order_ref     TEXT,                 -- only if the user volunteers it
    advertised    INTEGER NOT NULL DEFAULT 0,
    expected      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL CHECK (status IN ('expected', 'tracked', 'pending', 'confirmed', 'paid', 'rejected')),
    amount        INTEGER NOT NULL DEFAULT 0,  -- amount at current status (pending/confirmed/paid/rejected)
    expected_by   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    evidence      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE cashback_events (
    id          INTEGER PRIMARY KEY,
    record_id   INTEGER NOT NULL REFERENCES cashback_records(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    at          TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER REFERENCES products(id) ON DELETE CASCADE,
    reason          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL UNIQUE,  -- same product + same offer set + same price == same alert
    payload         TEXT NOT NULL,         -- JSON rendered message parts
    savings         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'deferred', 'sent', 'failed', 'suppressed')),
    channel         TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    sent_at         TEXT
);
CREATE INDEX idx_alerts_status ON alerts(status, next_attempt_at);
CREATE INDEX idx_alerts_product_time ON alerts(product_id, created_at);

CREATE TABLE job_leases (
    job        TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE job_runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_job_runs_job_time ON job_runs(job, started_at);
