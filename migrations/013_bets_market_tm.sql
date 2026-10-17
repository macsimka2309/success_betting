-- Рынок ТМ (тотал меньше) для ставок реестра (specs/реестр-ставок.md).
-- Расширяется проверка допустимых значений market; существующие строки не меняются.
ALTER TABLE bets DROP CONSTRAINT bets_market_check;
ALTER TABLE bets ADD CONSTRAINT bets_market_check CHECK (market IN ('TB', 'TM', 'ITB1', 'ITB2'));
