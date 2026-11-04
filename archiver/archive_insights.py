"""Offline derivations from archived public chat and public VOD metadata."""
from collections import Counter, defaultdict
import datetime as dt
import fcntl
import hashlib
import json
import re
import sqlite3
import time

import archive as a
import chat_archive as chat

SCHEMA = 2
UNAVAILABLE = ['historical_viewer_counts', 'private_streamer_analytics', 'complete_subscription_history',
               'complete_raid_history', 'moderation_history', 'deleted_messages', 'private_messages', 'donation_history']


def observe(db, meta, profile):
    db.execute('''CREATE TABLE IF NOT EXISTS vod_observation (
        seq INTEGER PRIMARY KEY, vod TEXT NOT NULL, at REAL NOT NULL,
        fingerprint TEXT NOT NULL, data TEXT NOT NULL)''')
    snapshot = {'video': meta, 'channel_snapshot': {k: profile.get(k) for k in ('id','login','displayName','followers','roles')},
                'scope': 'public snapshot at observation time, not historical live audience'}
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    previous = db.execute('SELECT fingerprint FROM vod_observation WHERE vod=? ORDER BY seq DESC LIMIT 1', (meta['id'],)).fetchone()
    if not previous or previous[0] != digest:
        db.execute('INSERT INTO vod_observation(vod,at,fingerprint,data) VALUES(?,?,?,?)', (meta['id'], time.time(), digest, blob))


def actions(node):
    text = chat.text_message(node)
    if match := re.match(r'^\s*(![\w-]{1,50})\b', text):
        yield 'chat.command', {'command': match[1].lower(), 'recognition': 'text_pattern_not_execution'}
    for name in sorted(set(re.findall(r'(?<!\w)@([A-Za-z0-9_]{1,25})', text))):
        yield 'chat.mention', {'login': name.lower(), 'recognition': 'text_pattern'}
    for url in sorted(set(re.findall(r'https?://[^\s<>]+', text))):
        yield 'chat.link', {'url': url, 'recognition': 'text_pattern_not_fetched'}
    for fragment in node['message']['fragments']:
        emote = fragment.get('emote')
        if emote:
            yield 'chat.emote', {'emote': emote, 'text': fragment.get('text'), 'recognition': 'twitch_fragment'}
    message = node['message']
    if message.get('is_action') is True or message.get('messageType') == 'ACTION':
        yield 'chat.action', {'recognition': 'explicit_source_field'}
    # Preserve typed notices only when present; never infer paid events from text.
    for key in ('noticeType','notice_type','userNotice','usernotice','bitsSpent'):
        if key in message:
            yield 'chat.source_field', {'field': key, 'value': message[key], 'recognition': 'explicit_source_field'}


def signature(row, dest):
    complete = json.loads((dest / 'chat.complete.json').read_text())
    details = (dest / 'details.json').read_text() if (dest / 'details.json').exists() else '{}'
    data = json.dumps([SCHEMA, row['meta'], complete['files']['chat.json']['sha256'], details], ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest(), complete, json.loads(details)


def process(queue_db, row):
    dest = a.folder(row)
    if not (dest / 'chat.complete.json').exists():
        return None
    with (dest / 'insights.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _process(queue_db, row, dest)


def _process(queue_db, row, dest):
    a.guard()
    fingerprint, complete, details = signature(row, dest)
    try:
        observed = queue_db.execute('SELECT coalesce(max(seq),0) FROM vod_observation WHERE vod=?',(row['id'],)).fetchone()[0]
    except sqlite3.OperationalError:
        observed = 0
    fingerprint = hashlib.sha256((fingerprint+':'+str(observed)).encode()).hexdigest()
    marker_path = dest / 'insights.complete.json'
    if marker_path.exists():
        old = json.loads(marker_path.read_text())
        if old.get('source_signature') == fingerprint and all((dest / name).is_file() and (dest / name).stat().st_size == item['bytes'] and a.checksum(dest / name) == item['sha256'] for name,item in old.get('files',{}).items()):
            return old
    meta = json.loads(row['meta'])
    db = sqlite3.connect('file:' + str(dest / 'chat.sqlite') + '?mode=ro', uri=True)
    try:
        users, commands, emotes, badges, action_types = Counter(), Counter(), Counter(), Counter(), Counter()
        user_names, minutes = {}, defaultdict(lambda: {'messages': 0, 'users': set()})
        message_count = action_count = links = mentions = 0
        first_offset = last_offset = None
        created = dt.datetime.fromisoformat(meta['createdAt'].replace('Z','+00:00'))
        chapters = details.get('chapters') or []
        coverage = {'chat': 'all_available_replay_messages', 'events': 'chat messages, public chapters and metadata-derived boundaries',
                    'actions': 'explicit source fields and labelled text patterns; not proof of command execution',
                    'public_metrics': 'current observed snapshot', 'unavailable_from_this_source': UNAVAILABLE}

        def nodes():
            for (raw,) in db.execute('SELECT raw FROM message ORDER BY offset,created,id'):
                a.guard()
                a.enough_space()
                yield json.loads(raw)

        def write_events(f):
            nonlocal message_count, action_count, links, mentions, first_offset, last_offset
            start = {'event_id': row['id']+':start', 'type': 'stream.start', 'offset_seconds': 0,
                     'created_at_utc': meta['createdAt'], 'source': 'vod_metadata', 'derived': True}
            extras = [start]
            for i, chapter in enumerate(chapters):
                extras.append({'event_id':row['id']+':chapter:'+str(i), 'type':'video.chapter',
                               'offset_seconds':chapter.get('start_time',0), 'source':'archived_extractor_metadata', 'data':chapter})
            extras.append({'event_id':row['id']+':end','type':'stream.end','offset_seconds':row['duration'],
                           'created_at_utc':(created+dt.timedelta(seconds=row['duration'])).isoformat(),
                           'source':'vod_start_plus_duration','derived':True})
            extras.sort(key=lambda event:event['offset_seconds'])
            pending=iter(extras)
            extra=next(pending,None)
            for node in nodes():
                offset = node['contentOffsetSeconds']
                while extra is not None and extra['offset_seconds'] <= offset:
                    f.write(json.dumps(extra,ensure_ascii=False)+'\n')
                    extra=next(pending,None)
                identity = node.get('commenter') or {}
                uid = identity.get('id') or 'unknown'
                users[uid] += 1
                user_names[uid] = identity
                bucket = minutes[int(max(0,offset)//60)]
                bucket['messages'] += 1
                bucket['users'].add(uid)
                message_count += 1
                first_offset = offset if first_offset is None else first_offset
                last_offset = offset
                for badge in node['message'].get('userBadges') or []:
                    badges[str(badge.get('setID') or badge.get('id'))] += 1
                for kind, payload in actions(node):
                    action_count += 1
                    action_types[kind] += 1
                    if kind == 'chat.command': commands[payload['command']] += 1
                    if kind == 'chat.emote': emotes[str(payload['emote'].get('emoteID') or payload['emote'].get('id'))] += 1
                    if kind == 'chat.link': links += 1
                    if kind == 'chat.mention': mentions += 1
                event = {'event_id': node['id'], 'type': 'chat.message', 'offset_seconds': offset,
                         'created_at_utc': node['createdAt'], 'source': 'twitch_vod_replay', 'derived': False, 'data': node}
                f.write(json.dumps(event,ensure_ascii=False)+'\n')
            while extra is not None:
                f.write(json.dumps(extra,ensure_ascii=False)+'\n')
                extra=next(pending,None)

        def write_actions(f):
            for node in nodes():
                for index,(kind,payload) in enumerate(actions(node)):
                    event = {'action_id':node['id']+':'+str(index), 'message_id':node['id'], 'type':kind,
                             'offset_seconds':node['contentOffsetSeconds'], 'created_at_utc':node['createdAt'],
                             'actor':node.get('commenter'), 'data':payload}
                    f.write(json.dumps(event,ensure_ascii=False)+'\n')

        files = {'events.jsonl': chat.checked_file(dest/'events.jsonl', write_events),
                 'actions.jsonl': chat.checked_file(dest/'actions.jsonl', write_actions)}
        if message_count != complete['message_count']:
            raise ValueError('canonical chat differs from completion count')
        timeline = [{'minute':minute,'messages':entry['messages'],'unique_users':len(entry['users'])} for minute,entry in sorted(minutes.items())]
        peak = max(timeline,key=lambda x:x['messages']) if timeline else None
        try:
            observations = [{'observed_at':at,'data':json.loads(data)} for at,data in queue_db.execute('SELECT at,data FROM vod_observation WHERE vod=? ORDER BY seq',(row['id'],))]
        except sqlite3.OperationalError:
            observations = []
        metrics = {'schema':'twitch-insights-v1','vod_id':row['id'],'generated_at':time.time(), 'coverage':coverage,
                   'public_video_snapshot':meta, 'public_observations':observations,
                   'chat':{'messages':message_count,'unique_users':len(users)-int('unknown' in users),'unknown_author_messages':users.get('unknown',0),'messages_per_minute':message_count/max(1,row['duration']/60),
                           'first_offset_seconds':first_offset,'last_offset_seconds':last_offset,'timeline':timeline,'peak_minute':peak,
                           'top_users':[dict(user=user_names[uid],messages=n) for uid,n in users.most_common(30)],
                           'commands':dict(commands),'emotes':dict(emotes),'message_badge_occurrences':dict(badges),
                           'links':links,'mentions':mentions},
                   'events':{'count':message_count+len(chapters)+2,'chapters':len(chapters)},
                   'actions':{'count':action_count,'types':dict(action_types)}}
        files['metrics.json'] = chat.checked_file(dest/'metrics.json',lambda f:json.dump(metrics,f,ensure_ascii=False,indent=2))
        result = {'schema':SCHEMA,'vod_id':row['id'],'source_signature':fingerprint,'generated_at':time.time(),
                  'messages':message_count,'events':metrics['events']['count'],'actions':action_count,
                  'unique_users':len(users)-int('unknown' in users),'files':files,'coverage':coverage}
        a.atomic_json(marker_path,result)
        return result
    finally:
        db.close()


def tick(db):
    for row in db.execute("SELECT * FROM vod WHERE status='done' ORDER BY CAST(id AS INTEGER) DESC").fetchall():
        a.guard()
        try:
            result=process(db,row)
            if result: a.log.info('INSIGHTS vod=%s messages=%s events=%s actions=%s',row['id'],result['messages'],result['events'],result['actions'])
        except a.Paused:
            raise
        except Exception as e:
            a.log.warning('insights vod=%s reason=%s',row['id'],type(e).__name__)
