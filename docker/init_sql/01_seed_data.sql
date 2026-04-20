-- Seed data for development/testing
INSERT INTO users (username, email, source)
VALUES ('test_user', 'test@chatsentry.dev', 'real')
ON CONFLICT (username) DO NOTHING;
