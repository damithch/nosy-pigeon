-- The pigeon's loft: where it keeps what it has seen.
-- Lives in its own schema, which is not exposed through the project's web API.
-- Row level security is on with no policies, so only the owner connection used by the pigeon can touch it.

create schema if not exists pigeon;
revoke all on schema pigeon from public, anon, authenticated;

-- windows the pigeon watches
create table if not exists pigeon.windows (
  key        text primary key,
  label      text not null,
  kind       text not null,
  updated_at timestamptz not null default now()
);

-- one row per flight (run)
create table if not exists pigeon.flights (
  id         bigint generated always as identity primary key,
  flown_at   timestamptz not null default now(),
  checks     int not null default 0,
  errors     int not null default 0,
  fresh      int not null default 0,
  cooed      int not null default 0
);

-- one row per window visited in a flight
create table if not exists pigeon.visits (
  id         bigint generated always as identity primary key,
  flight_id  bigint references pigeon.flights(id) on delete cascade,
  window_key text not null,
  at         timestamptz not null default now(),
  ok         boolean not null,
  items      int,
  error      text
);
create index if not exists visits_window_at on pigeon.visits (window_key, at desc);
create index if not exists visits_flight on pigeon.visits (flight_id);

-- everything the pigeon has spotted
create table if not exists pigeon.crumbs (
  fp            text primary key,          -- same fingerprint as seen.json
  window_key    text not null,
  title         text not null,
  org           text,
  place         text,
  kind          text,
  link          text,
  published_at  timestamptz,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  liked         boolean,                   -- passed the filters
  stars         smallint,
  tags          text[],
  cooed_at      timestamptz                -- when it was sent
);
create index if not exists crumbs_first_seen on pigeon.crumbs (first_seen_at desc);
create index if not exists crumbs_cooed on pigeon.crumbs (cooed_at desc) where cooed_at is not null;

-- what the perch did with a coo (button taps)
create table if not exists pigeon.pecks (
  id         bigint generated always as identity primary key,
  fp         text not null references pigeon.crumbs(fp) on delete cascade,
  who        text not null,
  verdict    text not null,
  at         timestamptz not null default now(),
  unique (fp, who)
);

-- small key/value notes (e.g. where it left off reading taps)
create table if not exists pigeon.notes (
  key   text primary key,
  value jsonb not null,
  at    timestamptz not null default now()
);

alter table pigeon.windows enable row level security;
alter table pigeon.flights enable row level security;
alter table pigeon.visits  enable row level security;
alter table pigeon.crumbs  enable row level security;
alter table pigeon.pecks   enable row level security;
alter table pigeon.notes   enable row level security;
revoke all on all tables in schema pigeon from public, anon, authenticated;
