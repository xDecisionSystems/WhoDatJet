-- Look up current registrations by Mode S address (GET /ui/mode-s).
CREATE INDEX registration_record_mode_s ON registration_record(mode_s_code_hex)
  WHERE source_file = 'master' AND mode_s_code_hex IS NOT NULL;
