-- Add web search and off-pod fetch providers to the model catalog.
--
-- Keep this transactional: replacing the inline CHECK is one atomic schema
-- change, so no row can be written between the DROP and ADD statements.

ALTER TABLE models
    DROP CONSTRAINT IF EXISTS models_capabilities_check;

ALTER TABLE models
    ADD CONSTRAINT models_capabilities_check CHECK (
        cardinality(capabilities) >= 1
        AND capabilities <@ ARRAY[
            'chat', 'auxiliary', 'embedding', 'vision', 'whisper', 'tts',
            'search', 'fetch'
        ]::TEXT[]
    );
