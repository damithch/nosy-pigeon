-- Following a crumb after it was pecked: how it went, and gentle reminders.
alter table pigeon.applications add column if not exists status_at timestamptz;
alter table pigeon.applications add column if not exists nudged_at timestamptz;   -- last time the perch was asked how it went
alter table pigeon.crumbs add column if not exists closes_on date;                 -- when the window says it closes
create index if not exists crumbs_closes_on on pigeon.crumbs (closes_on) where closes_on is not null;
create index if not exists crumbs_cooed_at on pigeon.crumbs (cooed_at) where cooed_at is not null;
