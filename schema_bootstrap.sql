-- ==============================================================================
-- Samantha Core — Schema Database Bootstrap PostgreSQL
-- ==============================================================================

-- 1. Tabella principale degli Agenti (Registry di Flotta)
CREATE TABLE IF NOT EXISTS agents (
    agent_id VARCHAR(255) PRIMARY KEY,
    data JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Indici di performance su campi JSONB ad alto traffico
CREATE INDEX IF NOT EXISTS idx_agents_studio ON agents ((data->>'studio'));
CREATE INDEX IF NOT EXISTS idx_agents_dismesso ON agents ((data->>'dismesso'));
CREATE INDEX IF NOT EXISTS idx_agents_coordinatore ON agents ((data->>'coordinatore'));
CREATE INDEX IF NOT EXISTS idx_agents_tmux_window ON agents ((data->>'tmux_window'));
CREATE INDEX IF NOT EXISTS idx_agents_creato_da ON agents ((data->>'creato_da'));
CREATE INDEX IF NOT EXISTS idx_agents_always_on ON agents ((data->>'always_on'));

-- 2. Registro Audit e Ledger Messaggi A2A (Agent-to-Agent)
CREATE TABLE IF NOT EXISTS a2a_silent_logs (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    sender VARCHAR(255),
    recipient VARCHAR(255),
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_a2a_logs_sender ON a2a_silent_logs(sender);
CREATE INDEX IF NOT EXISTS idx_a2a_logs_recipient ON a2a_silent_logs(recipient);
CREATE INDEX IF NOT EXISTS idx_a2a_logs_timestamp ON a2a_silent_logs(timestamp);

-- 3. Registro degli Studi
CREATE TABLE IF NOT EXISTS studi (
    nome TEXT PRIMARY KEY,
    stato TEXT DEFAULT 'attivo',
    aggiornato_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Inserimento Studio Demo e Coordinatore Iniziale
INSERT INTO studi (nome, stato) VALUES ('studio_demo', 'attivo')
ON CONFLICT (nome) DO NOTHING;

INSERT INTO agents (agent_id, data) VALUES (
    'coord_demo',
    jsonb_build_object(
        'name', 'Coordinatore Demo',
        'studio', 'studio_demo',
        'role', 'Coordinatore dello Studio Demo',
        'engine', 'agy',
        'model', 'cascata-fast',
        'always_on', true,
        'tmux_window', 'studio_demo:AGY-COORD_DEMO',
        'work_dir', 'studios/studio_demo',
        'coordinatore', 'coord_demo',
        'dismesso', false
    )
)
ON CONFLICT (agent_id) DO NOTHING;
