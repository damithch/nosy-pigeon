-- What each perch actually went for, with a short summary of the ad kept for later.
create table if not exists pigeon.applications (
  id            bigint generated always as identity primary key,
  fp            text not null references pigeon.crumbs(fp) on delete cascade,
  who           text not null,
  title         text not null,
  org           text,
  link          text,
  applied_at    timestamptz not null default now(),
  status        text not null default 'applied',
  summary_company text,
  summary_role    text,
  skills          text[],
  closing         text,
  raw_text        text,
  summary_state   text not null default 'pending',   -- pending | done | failed
  attempts        int not null default 0,
  updated_at      timestamptz not null default now(),
  unique (fp, who)
);
create index if not exists applications_who on pigeon.applications (who, applied_at desc);

-- what Gemini thought of each crumb it was asked about (one answer per crumb)
create table if not exists pigeon.opinions (
  fp        text primary key references pigeon.crumbs(fp) on delete cascade,
  fit       text not null,          -- strong | maybe | no
  stars     smallint,
  reason    text,
  at        timestamptz not null default now()
);

alter table pigeon.applications enable row level security;
alter table pigeon.opinions enable row level security;
revoke all on pigeon.applications, pigeon.opinions from public, anon, authenticated;
grant select on pigeon.applications, pigeon.opinions to pigeon_viewer;
drop policy if exists seat_reads on pigeon.applications;
create policy seat_reads on pigeon.applications for select to pigeon_viewer using (true);
drop policy if exists seat_reads on pigeon.opinions;
create policy seat_reads on pigeon.opinions for select to pigeon_viewer using (true);

-- every Applied tap so far becomes an application
insert into pigeon.applications (fp, who, title, org, link, applied_at)
select p.fp, p.who, c.title, c.org, c.link, p.at
from pigeon.pecks p join pigeon.crumbs c using (fp)
where p.verdict = 'done' and c.window_key <> 'practice'
on conflict (fp, who) do nothing;
