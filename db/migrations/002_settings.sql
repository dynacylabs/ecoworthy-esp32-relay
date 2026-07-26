-- Runtime-configurable settings, editable from the web UI's Settings tab
-- (see app/settings.py). Every key is optional here - unset means "use
-- the env-var default from config.py". A row only exists once a user has
-- explicitly overridden that default via the Settings page.
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
