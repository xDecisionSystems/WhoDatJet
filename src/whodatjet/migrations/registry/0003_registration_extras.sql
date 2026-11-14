-- More FAA MASTER columns (owner decision 2026-11-14): co-owner names, airworthiness date, certification class,
-- fractional ownership, kit manufacturer and model, region and county.
ALTER TABLE registration_record ADD COLUMN certification TEXT;
ALTER TABLE registration_record ADD COLUMN fractional_owner TEXT;
ALTER TABLE registration_record ADD COLUMN airworthiness_date TEXT;
ALTER TABLE registration_record ADD COLUMN other_names TEXT;          -- JSON array of the "other names" fields
ALTER TABLE registration_record ADD COLUMN kit_manufacturer TEXT;
ALTER TABLE registration_record ADD COLUMN kit_model TEXT;
ALTER TABLE registration_record ADD COLUMN region TEXT;
ALTER TABLE registration_record ADD COLUMN county TEXT;

-- Record hashes now cover the new columns, so a database that already holds records must be rebuilt from the
-- retained archives (`whodatjet rebuild`, which ingest and the updater run by themselves). A new, empty
-- database has nothing to rebuild.
INSERT OR REPLACE INTO meta (key, value)
SELECT 'needs_rebuild', 'migration 0003 added FAA columns; rebuild from the archives to fill them'
WHERE EXISTS (SELECT 1 FROM registration_record);
