-- AI Desk Bot relay: live state for the phone app and commands back to whoever runs the bot (PC or GitHub).
-- Every table is closed to the API (RLS on, no policies, no grants); access is only through the
-- SECURITY DEFINER functions below, which check either the runner secret or an app session.
-- Run once in the Supabase SQL editor of the "ai-desk-bot" project.
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.kv (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
create table if not exists public.commands (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  kind text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'new',
  handled_by text,
  handled_at timestamptz,
  result jsonb
);
create table if not exists public.secrets (
  name text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);
create table if not exists public.sessions (
  token_hash text primary key,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);
create table if not exists public.login_failures (
  at timestamptz not null default now(),
  ip text
);

alter table public.kv enable row level security;
alter table public.commands enable row level security;
alter table public.secrets enable row level security;
alter table public.sessions enable row level security;
alter table public.login_failures enable row level security;
revoke all on public.kv, public.commands, public.secrets, public.sessions, public.login_failures from anon, authenticated;
revoke all on sequence public.commands_id_seq from anon, authenticated;

create or replace function public._sha256(p text) returns text
language sql immutable set search_path = '' as
$$ select encode(extensions.digest(convert_to(coalesce(p, ''), 'UTF8'), 'sha256'), 'hex') $$;

create or replace function public._runner_ok(p_secret text) returns boolean
language sql stable security definer set search_path = '' as
$$ select exists (select 1 from public.secrets s where s.name = 'runner_hash' and s.value = public._sha256(p_secret)) $$;

create or replace function public._session_ok(p_token text) returns boolean
language sql stable security definer set search_path = '' as
$$ select exists (select 1 from public.sessions s where s.token_hash = public._sha256(p_token) and s.expires_at > now()) $$;

-- runners -------------------------------------------------------------------------------
create or replace function public.runner_push(p_secret text, p_items jsonb) returns integer
language plpgsql security definer set search_path = '' as $$
declare k text; v jsonb; n integer := 0;
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  for k, v in select * from jsonb_each(p_items) loop
    insert into public.kv(key, value, updated_at) values (k, v, now())
      on conflict (key) do update set value = excluded.value, updated_at = now();
    n := n + 1;
  end loop;
  return n;
end $$;

create or replace function public.runner_get(p_secret text, p_prefix text) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  return coalesce((select jsonb_object_agg(key, jsonb_build_object('value', value, 'updated_at', updated_at))
                   from public.kv where key like p_prefix || '%'), '{}'::jsonb);
end $$;

create or replace function public.runner_take(p_secret text, p_runner text) returns setof public.commands
language plpgsql security definer set search_path = '' as $$
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  return query update public.commands set status = 'taken', handled_by = p_runner, handled_at = now()
    where id in (select c.id from public.commands c where c.status = 'new' order by c.id for update skip locked)
    returning *;
end $$;

create or replace function public.runner_done(p_secret text, p_id bigint, p_result jsonb) returns void
language plpgsql security definer set search_path = '' as $$
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  update public.commands set status = 'done', result = p_result where id = p_id;
  delete from public.commands where status = 'done' and created_at < now() - interval '7 days';
end $$;

create or replace function public.runner_set_password(p_secret text, p_salt text, p_iterations integer, p_hash text) returns void
language plpgsql security definer set search_path = '' as $$
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  insert into public.secrets(name, value) values ('pw_salt', p_salt), ('pw_iterations', p_iterations::text), ('pw_hash', p_hash)
    on conflict (name) do update set value = excluded.value, updated_at = now();
end $$;

-- phone app -----------------------------------------------------------------------------
create or replace function public.app_state(p_token text, p_since timestamptz default null) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
  if not public._session_ok(p_token) then raise exception 'login needed' using errcode = '28000'; end if;
  return jsonb_build_object('now', now(), 'items',
    coalesce((select jsonb_object_agg(key, jsonb_build_object('value', value, 'updated_at', updated_at))
              from public.kv where key like 'app:%' and (p_since is null or updated_at > p_since)), '{}'::jsonb));
end $$;

create or replace function public.app_command(p_token text, p_kind text, p_payload jsonb) returns bigint
language plpgsql security definer set search_path = '' as $$
declare new_id bigint;
begin
  if not public._session_ok(p_token) then raise exception 'login needed' using errcode = '28000'; end if;
  if p_kind not in ('pause', 'start_session', 'stop_session') then raise exception 'unknown command'; end if;
  if (select count(*) from public.commands where created_at > now() - interval '1 minute') > 20 then
    raise exception 'too many commands';
  end if;
  insert into public.commands(kind, payload) values (p_kind, coalesce(p_payload, '{}'::jsonb)) returning id into new_id;
  return new_id;
end $$;

revoke all on function public._runner_ok(text), public._session_ok(text) from public, anon, authenticated;
revoke all on function public.runner_push(text, jsonb), public.runner_get(text, text), public.runner_take(text, text),
  public.runner_done(text, bigint, jsonb), public.runner_set_password(text, text, integer, text),
  public.app_state(text, timestamptz), public.app_command(text, text, jsonb) from public, authenticated;
grant execute on function public.runner_push(text, jsonb), public.runner_get(text, text), public.runner_take(text, text),
  public.runner_done(text, bigint, jsonb), public.runner_set_password(text, text, integer, text),
  public.app_state(text, timestamptz), public.app_command(text, text, jsonb) to anon;

-- runner secret (only its sha256 is stored)
insert into public.secrets(name, value) values ('runner_hash', 'd0e039dedf153e9be6b61f76da2865f7d45f692663d9ea8ac571d37ecc5c6c39')
  on conflict (name) do update set value = excluded.value, updated_at = now();
