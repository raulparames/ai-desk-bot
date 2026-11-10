-- AI Desk Bot relay, "Gráfico" desk: the phone hands chart screenshots to the PC and reads the answer back.
-- Run after supabase_schema.sql. The analysis itself happens on the PC (Claude there); Supabase only carries it.
-- The whole file can be run again: the functions are replaced and the timer keeps its one name.
--
-- Why two keys outside the "app:" prefix: app_state() sends every changed "app:%" row to every poll, and a
-- screenshot is hundreds of KB. "chart:in" is read once by the runner and then blanked; "chart:out" is small
-- and has its own reader, so the page never depends on the rolling p_since cursor of app_state().
--
-- A screenshot nobody came for (computer off) is blanked after 15 minutes: when the phone polls
-- app_chart_get, and every 5 minutes by pg_cron (job 'ai-desk-chart-expire'), phone open or not.
create extension if not exists pg_cron;

create or replace function public.app_chart(p_token text, p_shots jsonb, p_note text default '') returns bigint
language plpgsql security definer set search_path = '' as $$
declare new_id bigint; s jsonb; clean jsonb := '[]'::jsonb;
begin
  if not public._session_ok(p_token) then raise exception 'login needed' using errcode = '28000'; end if;
  if p_shots is null or jsonb_typeof(p_shots) <> 'array' or jsonb_array_length(p_shots) not between 1 and 3 then
    raise exception 'bad shots';
  end if;
  for s in select * from jsonb_array_elements(p_shots) loop
    if jsonb_typeof(s) <> 'object' or jsonb_typeof(s -> 'image') is distinct from 'string'
       or coalesce(s ->> 'tf', 'auto') not in ('1m', '5m', '15m', 'auto') then
      raise exception 'bad shots';
    end if;
    -- 400 000 bytes of image are 533 336 base64 characters; the PC checks the decoded size again.
    if length(s ->> 'image') > 540000 then raise exception 'too large'; end if;
    clean := clean || jsonb_build_array(jsonb_build_object('tf', coalesce(s ->> 'tf', 'auto'), 'image', s ->> 'image'));
  end loop;
  -- Same overall brake as app_command, plus one for this desk: each analysis is a paid model call on the PC.
  -- Two names, so the phone can say which one it hit: the first passes in a minute, the second in up to ten.
  if (select count(*) from public.commands where created_at > now() - interval '1 minute') > 20 then
    raise exception 'too many commands';
  end if;
  if (select count(*) from public.commands where kind = 'analyze_chart' and created_at > now() - interval '10 minutes') >= 8 then
    raise exception 'too many charts';
  end if;
  insert into public.commands(kind, payload) values ('analyze_chart', '{}'::jsonb) returning id into new_id;
  insert into public.kv(key, value, updated_at)
    values ('chart:in', jsonb_build_object('id', new_id, 'shots', clean, 'note', left(coalesce(p_note, ''), 200), 'at', now()), now())
    on conflict (key) do update set value = excluded.value, updated_at = now();
  return new_id;
end $$;

create or replace function public.app_chart_get(p_token text) returns jsonb
language plpgsql security definer set search_path = '' as $$
begin
  if not public._session_ok(p_token) then raise exception 'login needed' using errcode = '28000'; end if;
  -- A screenshot nobody came for (computer off) is not kept: blank it after 15 minutes (the timer below does
  -- the same when no phone is polling).
  update public.kv set value = jsonb_build_object('gone', true, 'at', extract(epoch from now())), updated_at = now()
    where key = 'chart:in' and updated_at < now() - interval '15 minutes' and not (value ? 'gone');
  return jsonb_build_object(
    'now', now(),
    'chart', (select value from public.kv where key = 'chart:out'),
    'runner', (select jsonb_build_object('value', value, 'updated_at', updated_at) from public.kv where key = 'app:runner'));
end $$;

revoke all on function public.app_chart(text, jsonb, text), public.app_chart_get(text) from public, authenticated;
grant execute on function public.app_chart(text, jsonb, text), public.app_chart_get(text) to anon;

-- The same expiry as in app_chart_get, on a timer: with the phone closed nobody polls. Scheduling under a
-- name that exists replaces that job (as 'ai-desk-watch' in supabase_watch.sql), so this can be run again.
select cron.schedule('ai-desk-chart-expire', '*/5 * * * *', $$
  update public.kv set value = jsonb_build_object('gone', true, 'at', extract(epoch from now())), updated_at = now()
    where key = 'chart:in' and updated_at < now() - interval '15 minutes' and not (value ? 'gone')
$$);
