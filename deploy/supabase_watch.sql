-- AI Desk Bot: the phone hears it when the runner of a session goes quiet (see deskbot/watch.py).
--
-- Each runner keeps its own switch: kv 'watch:arm:pc' or 'watch:arm:github', rewritten (runner_push) every
-- minute while one of its sessions is alive, with notifications already encrypted and signed by the runner.
-- The row's own date is the runner's sign of life. pg_cron runs watch_tick() every minute: an active arm
-- not rewritten for 180 s is a silence. The first to claim it tells it ('watch:fired:<runner>'): Supabase
-- (here, then the edge function `watch` posts the copy taken at the claim) or GitHub's 10-minute cycle
-- (runner_watch_claim, then runner_watch_result). A silence counts as told only once a push service took
-- it: Supabase tries three times, then leaves it to GitHub; GitHub releases a claim it could not deliver.
--
-- Applied after supabase_schema.sql. No key lives here. The edge function `watch` is deployed with JWT
-- verification OFF (pg_net calls it without a token); it only sends what a claim made due.
create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

-- who tells one silence (an arm's gen): the first caller. Returns the claim, without the requests.
create or replace function public._watch_claim(p_runner text, p_gen text, p_by text) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare arm jsonb;
begin
  if p_runner not in ('pc', 'github') or p_by not in ('supabase', 'github') or coalesce(p_gen, '') = '' then
    return null;
  end if;
  select value into arm from public.kv where key = 'watch:arm:' || p_runner;
  if arm->>'gen' = p_gen then         -- the copy: what is told no longer depends on what the runner writes next
    insert into public.kv(key, value, updated_at)
      values ('watch:fired:' || p_runner,
              jsonb_build_object('gen', p_gen, 'by', p_by, 'at', now(), 'tries', 1,
                                 'desks', coalesce(arm->'desks', '[]'::jsonb), 'sends', coalesce(arm->'sends', '[]'::jsonb)),
              now())
      on conflict (key) do update set value = excluded.value, updated_at = now()
      where public.kv.value->>'gen' is distinct from p_gen;
  end if;
  return (select value - 'sends' from public.kv where key = 'watch:fired:' || p_runner and value->>'gen' = p_gen);
end $$;

create or replace function public.runner_watch_claim(p_secret text, p_runner text, p_gen text) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  return public._watch_claim(p_runner, p_gen, 'github');
end $$;

-- GitHub sent its own notification for a silence it claimed: told, or released for Supabase to try
create or replace function public.runner_watch_result(p_secret text, p_runner text, p_gen text, p_delivered boolean)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if not public._runner_ok(p_secret) then raise exception 'not allowed' using errcode = '42501'; end if;
  if p_delivered then
    update public.kv set value = (value - 'sends') || jsonb_build_object('delivered', true, 'sent_at', now()), updated_at = now()
      where key = 'watch:fired:' || p_runner and value->>'gen' = p_gen and value->>'by' = 'github';
  else
    delete from public.kv where key = 'watch:fired:' || p_runner and value->>'gen' = p_gen and value->>'by' = 'github';
  end if;
end $$;

-- the edge function takes one send (true) and writes what the push services answered
create or replace function public._watch_sending(p_runner text, p_gen text) returns boolean
language plpgsql security definer set search_path = '' as $$
declare n integer;
begin
  update public.kv set value = value || jsonb_build_object('sending_at', now()), updated_at = now()
    where key = 'watch:fired:' || p_runner and value->>'gen' = p_gen and value->>'by' = 'supabase'
      and value->>'sending_at' is null and coalesce((value->>'delivered')::boolean, false) = false
      and coalesce((value->>'failed')::boolean, false) = false;
  get diagnostics n = row_count;
  return n > 0;
end $$;

create or replace function public._watch_sent(p_runner text, p_gen text, p_result jsonb, p_delivered boolean) returns void
language plpgsql security definer set search_path = '' as $$
begin
  update public.kv set value = (case when p_delivered then value - 'sends' else value end)
      || jsonb_build_object('sent_at', now(), 'result', p_result, 'delivered', p_delivered), updated_at = now()
    where key = 'watch:fired:' || p_runner and value->>'gen' = p_gen and value->>'by' = 'supabase';
end $$;

create or replace function public._watch_call(p_runner text, p_gen text) returns void
language plpgsql security definer set search_path = '' as $$
begin
  perform net.http_post(url := 'https://kkawnjfowemyqkplkrma.supabase.co/functions/v1/watch',
                        body := jsonb_build_object('runner', p_runner, 'gen', p_gen),
                        headers := '{"Content-Type": "application/json"}'::jsonb, timeout_milliseconds := 30000);
end $$;

create or replace function public.watch_tick() returns text
language plpgsql security definer set search_path = '' as $$
declare
  r text; arm jsonb; arm_at timestamptz; fired jsonb; gen text; until double precision; out text := '';
begin
  foreach r in array array['pc', 'github'] loop
    select value, updated_at into arm, arm_at from public.kv where key = 'watch:arm:' || r;
    if arm is null or jsonb_typeof(arm->'active') is distinct from 'boolean' or not (arm->>'active')::boolean then
      continue;
    end if;
    gen := arm->>'gen';
    until := case when jsonb_typeof(arm->'until') = 'number' then (arm->>'until')::double precision else 0 end;
    if coalesce(gen, '') = '' or until < extract(epoch from now()) then continue; end if;
    if arm_at > now() - interval '180 seconds' then continue; end if;          -- the runner rewrote it lately
    select value into fired from public.kv where key = 'watch:fired:' || r;
    if fired->>'gen' = gen then
      if fired->>'by' = 'supabase' and coalesce((fired->>'delivered')::boolean, false) = false
         and coalesce((fired->>'failed')::boolean, false) = false
         and (fired->>'at')::timestamptz < now() - interval '90 seconds' then
        if coalesce((fired->>'tries')::int, 1) >= 3 then                      -- given up: GitHub tells it
          update public.kv set value = value || '{"failed": true}'::jsonb, updated_at = now() where key = 'watch:fired:' || r;
        else
          update public.kv set value = (value - 'sending_at')
              || jsonb_build_object('tries', coalesce((fired->>'tries')::int, 1) + 1, 'at', now()), updated_at = now()
            where key = 'watch:fired:' || r;
          perform public._watch_call(r, gen);
          out := out || r || ':retry ';
        end if;
      end if;
      continue;
    end if;
    if public._watch_claim(r, gen, 'supabase')->>'by' = 'supabase' then
      perform public._watch_call(r, gen);
      out := out || r || ':fired ';
    end if;
  end loop;
  return coalesce(nullif(trim(out), ''), 'quiet');
end $$;

revoke all on function public._watch_claim(text, text, text), public._watch_sending(text, text),
  public._watch_sent(text, text, jsonb, boolean), public._watch_call(text, text), public.watch_tick(),
  public.runner_watch_claim(text, text, text), public.runner_watch_result(text, text, text, boolean)
  from public, anon, authenticated, service_role;
grant execute on function public.runner_watch_claim(text, text, text),
  public.runner_watch_result(text, text, text, boolean) to anon;                        -- they check the runner secret
grant execute on function public._watch_sending(text, text), public._watch_sent(text, text, jsonb, boolean)
  to service_role;                                                                        -- the edge function

select cron.schedule('ai-desk-watch', '* * * * *', $$select public.watch_tick()$$);
