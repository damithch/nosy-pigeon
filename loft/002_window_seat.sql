-- A read-only seat for the loft's window (the dashboard).
-- Its password is set separately and never kept in the repo:
--   alter role pigeon_viewer with login password '...';
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'pigeon_viewer') then
    create role pigeon_viewer nologin;
  end if;
end $$;

grant usage on schema pigeon to pigeon_viewer;
grant select on all tables in schema pigeon to pigeon_viewer;
alter default privileges in schema pigeon grant select on tables to pigeon_viewer;

-- row level security is on, so the seat needs its own read-only policies
do $$
declare t text;
begin
  foreach t in array array['windows','flights','visits','crumbs','pecks','notes'] loop
    execute format('drop policy if exists seat_reads on pigeon.%I', t);
    execute format('create policy seat_reads on pigeon.%I for select to pigeon_viewer using (true)', t);
  end loop;
end $$;
