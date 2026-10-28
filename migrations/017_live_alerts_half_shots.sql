-- Категория half_shots: владение во 2-м тайме больше 70 % и 2+ удара в створ (specs/онлайн-оповещения.md).
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check
    CHECK (category IN ('stats', 'half', 'half_attack', 'favorite', 'half_shots'));
