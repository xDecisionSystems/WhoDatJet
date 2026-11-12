-- Registry database baseline. Tables are added by later migrations (plan Phase 2).
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO meta (key, value) VALUES ('database', 'registry');
