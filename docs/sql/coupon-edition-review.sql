-- REVIEW DRAFT ONLY. Not registered with Base.metadata or application migrations.
-- Installed solely by tests/test_coupon_edition_store.py in a disposable schema.
-- __SCHEMA__ must be an allowlisted coupon_test_<32 hex> identifier.
CREATE TABLE __SCHEMA__.coupon_edition (
    storage_id uuid PRIMARY KEY,
    day date NOT NULL,
    kind text NOT NULL CHECK (kind IN ('banko','balanced','combined_6_7','combined_8_10')),
    revision integer NOT NULL CHECK (revision >= 1),
    content_digest text NOT NULL UNIQUE CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    edition_id text NOT NULL CHECK (edition_id = 'ce1:' || content_digest),
    previous_id text,
    first_id text NOT NULL,
    captured_as_of timestamptz NOT NULL,
    model_version text NOT NULL CHECK (length(model_version) BETWEEN 1 AND 100),
    -- Canonical content excludes only edition_id/content_digest; read reconstructs
    -- the original ce1 document exactly. No board, model inputs or nested history.
    content text NOT NULL CONSTRAINT coupon_content_byte_budget CHECK (octet_length(content) BETWEEN 2 AND 16384),
    UNIQUE (day, kind, revision),
    UNIQUE (day, kind, edition_id),
    FOREIGN KEY (day, kind, previous_id)
        REFERENCES __SCHEMA__.coupon_edition(day, kind, edition_id),
    FOREIGN KEY (day, kind, first_id)
        REFERENCES __SCHEMA__.coupon_edition(day, kind, edition_id),
    CONSTRAINT coupon_content_digest_matches CHECK (content_digest = encode(sha256(convert_to(content, 'UTF8')), 'hex')),
    CHECK (jsonb_typeof(content::jsonb) = 'object'),
    CHECK (content::jsonb ?& ARRAY['schema','day','kind','revision','previous_id',
        'first_edition_id','captured_as_of','model_version','items','state','reason','diff','combined_odds']),
    CHECK ((content::jsonb->>'schema') IS NOT DISTINCT FROM 'coupon-edition-v1'),
    CHECK ((content::jsonb->>'day') IS NOT DISTINCT FROM day::text),
    CHECK ((content::jsonb->>'kind') IS NOT DISTINCT FROM kind),
    CHECK ((content::jsonb->>'revision') IS NOT DISTINCT FROM revision::text),
    CHECK ((content::jsonb->>'previous_id') IS NOT DISTINCT FROM previous_id),
    CHECK ((content::jsonb->>'model_version') IS NOT DISTINCT FROM model_version),
    CHECK ((content::jsonb->>'captured_as_of')::timestamptz IS NOT DISTINCT FROM captured_as_of),
    CHECK ((captured_as_of AT TIME ZONE 'Europe/Istanbul')::date = day),
    CHECK (jsonb_typeof(content::jsonb->'items') IS NOT DISTINCT FROM 'array'),
    CHECK ((content::jsonb->>'state') IS NOT NULL AND (content::jsonb->>'state') IN ('published','no_eligible','incomplete')),
    CHECK (((content::jsonb->>'state') = 'published') = (jsonb_array_length(content::jsonb->'items') > 0)),
    CHECK ((revision = 1 AND previous_id IS NULL AND first_id = edition_id
                AND (content::jsonb->>'first_edition_id') IS NULL)
        OR (revision > 1 AND previous_id IS NOT NULL
                AND (content::jsonb->>'first_edition_id') IS NOT DISTINCT FROM first_id))
);

CREATE TABLE __SCHEMA__.coupon_head (
    day date NOT NULL,
    kind text NOT NULL,
    edition_id text NOT NULL,
    PRIMARY KEY (day, kind),
    FOREIGN KEY (day, kind, edition_id)
        REFERENCES __SCHEMA__.coupon_edition(day, kind, edition_id)
);

CREATE FUNCTION __SCHEMA__.reject_coupon_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'coupon editions are append-only; deletion/truncation is disabled'
        USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER coupon_edition_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.coupon_edition
FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.reject_coupon_mutation();
CREATE TRIGGER coupon_edition_no_truncate BEFORE TRUNCATE ON __SCHEMA__.coupon_edition
FOR EACH STATEMENT EXECUTE FUNCTION __SCHEMA__.reject_coupon_mutation();
CREATE TRIGGER coupon_head_no_delete BEFORE DELETE ON __SCHEMA__.coupon_head
FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.reject_coupon_mutation();
CREATE TRIGGER coupon_head_no_truncate BEFORE TRUNCATE ON __SCHEMA__.coupon_head
FOR EACH STATEMENT EXECUTE FUNCTION __SCHEMA__.reject_coupon_mutation();

CREATE FUNCTION __SCHEMA__.check_coupon_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE prior __SCHEMA__.coupon_edition%ROWTYPE;
BEGIN
    IF NEW.revision > 1 THEN
        SELECT * INTO prior FROM __SCHEMA__.coupon_edition WHERE edition_id = NEW.previous_id
            AND day = NEW.day AND kind = NEW.kind;
        IF NOT FOUND OR prior.revision + 1 <> NEW.revision OR prior.first_id <> NEW.first_id
            OR prior.captured_as_of > NEW.captured_as_of THEN
            RAISE EXCEPTION 'invalid coupon revision chain' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER coupon_edition_chain BEFORE INSERT ON __SCHEMA__.coupon_edition
FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.check_coupon_chain();

CREATE FUNCTION __SCHEMA__.check_coupon_head() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE target __SCHEMA__.coupon_edition%ROWTYPE;
BEGIN
    SELECT * INTO target FROM __SCHEMA__.coupon_edition
        WHERE day = NEW.day AND kind = NEW.kind AND edition_id = NEW.edition_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'missing coupon head target' USING ERRCODE = '23503';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF target.revision <> 1 THEN
            RAISE EXCEPTION 'head must begin at revision one' USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.day <> OLD.day OR NEW.kind <> OLD.kind OR target.previous_id IS DISTINCT FROM OLD.edition_id THEN
            RAISE EXCEPTION 'head must advance one revision within its scope' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER coupon_head_chain BEFORE INSERT OR UPDATE ON __SCHEMA__.coupon_head
FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.check_coupon_head();
