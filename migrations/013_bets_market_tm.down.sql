-- Откат 013: возвращает прежний набор рынков. Ставки на ТМ удаляются, иначе проверка не
-- применится; выполнять только по явному решению владельца.
DELETE FROM bets WHERE market = 'TM';
ALTER TABLE bets DROP CONSTRAINT bets_market_check;
ALTER TABLE bets ADD CONSTRAINT bets_market_check CHECK (market IN ('TB', 'ITB1', 'ITB2'));
