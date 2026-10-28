-- Every nest (company) the pigeon knows about, whether or not it can watch it yet.
create table if not exists pigeon.nests (
  name        text primary key,
  status      text not null,          -- watching | blocked | site_gone | not_useful | no_listings
  window_key  text,                   -- set when status = watching
  url         text,
  note        text,
  updated_at  timestamptz not null default now()
);
alter table pigeon.nests enable row level security;
revoke all on pigeon.nests from public, anon, authenticated;
grant select on pigeon.nests to pigeon_viewer;
drop policy if exists seat_reads on pigeon.nests;
create policy seat_reads on pigeon.nests for select to pigeon_viewer using (true);
