DELETE FROM transaction_splits;
DELETE FROM cc_payment_allocations;
DELETE FROM loan_payment_parts;
DELETE FROM investment_valuations;
DELETE FROM loan_statements;
DELETE FROM transactions;
DELETE FROM cc_budget_adjustments;
DELETE FROM credit_cards;
DELETE FROM loans;
DELETE FROM investment_accounts;
DELETE FROM envelopes;
DELETE FROM accounts;
DELETE FROM sqlite_sequence;
INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents, display_order) VALUES (1,'Checking','bank','acct:checking',0,1);
INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents, display_order) VALUES (2,'Savings','bank','acct:savings',0,2);
INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents, display_order) VALUES (3,'Visa Card','credit_card','acct:visa',0,3);
INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents, display_order) VALUES (4,'Brokerage','investment','acct:broker',0,4);
INSERT INTO accounts (id, name, account_type, acct_key, opening_balance_cents, display_order) VALUES (5,'Student Loan','loan','acct:loan',0,5);
INSERT INTO envelopes (id, name, locked_account_id, default_amount_cents) VALUES (1,'Groceries',NULL,0);
INSERT INTO envelopes (id, name, locked_account_id, default_amount_cents) VALUES (2,'Dining Out',NULL,0);
INSERT INTO envelopes (id, name, locked_account_id, default_amount_cents) VALUES (3,'Rent',NULL,0);
INSERT INTO envelopes (id, name, locked_account_id, default_amount_cents) VALUES (4,'Utilities',NULL,0);
INSERT INTO envelopes (id, name, locked_account_id, default_amount_cents) VALUES (5,'Travel',NULL,0);
INSERT INTO envelopes (id, name, locked_account_id, default_amount_cents) VALUES (6,'Emergency Fund',2,0);
INSERT INTO credit_cards (account_id, credit_limit_cents) VALUES (3,500000);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (1,1,'income',300000,'2025-09-01','Employer','Salary','FIT001',0,NULL,NULL);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (2,1,'expense',-150000,'2025-09-02','Landlord','Monthly rent','FIT002',0,NULL,NULL);
INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (2,3,-150000);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (3,1,'expense',-4500,'2025-09-03','Market','Groceries','FIT003',0,NULL,NULL);
INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (3,1,-4500);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (4,1,'transfer_out',-50000,'2025-09-05','To Savings','Monthly transfer','FIT004',0,5,NULL);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (5,2,'transfer_in',50000,'2025-09-05','From Checking','Monthly transfer','FIT005',0,4,NULL);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (6,3,'expense',-2500,'2025-09-06','Coffee Shop','Latte','FIT006',0,NULL,NULL);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (7,3,'expense',-4200,'2025-09-07','Restaurant','Dinner','FIT007',0,NULL,NULL);
INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (6,2,-2500);
INSERT INTO transaction_splits (transaction_id, envelope_id, amount_cents) VALUES (7,2,-4200);
INSERT INTO investment_valuations 
(id, account_id, asof_date, value_cents, source, note) 
VALUES (1,4,'2025-09-02','2500000','manual','Initial valuation');
INSERT INTO loans (account_id, original_principal_cents, note) VALUES (5,1000000,'Student loan account setup');
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (8,5,'expense',-20000,'2025-09-10','Loan Servicer','Payment','FIT008',0,NULL,NULL);
INSERT INTO transactions 
(id, account_id, ttype, amount_cents, posted_at, payee, memo, fitid, ignore_match, xfer_pair_id, external_counterparty)
VALUES (9,5,'expense',-20000,'2025-09-20','Loan Servicer','Payment','FIT009',0,NULL,NULL);