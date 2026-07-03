-- Enforce design rule: the event table (COP) is append-only. DECISIONS.md #6.

CREATE TRIGGER event_no_update BEFORE UPDATE ON event
BEGIN
  SELECT RAISE(ABORT, 'event is append-only');
END;

CREATE TRIGGER event_no_delete BEFORE DELETE ON event
BEGIN
  SELECT RAISE(ABORT, 'event is append-only');
END;
