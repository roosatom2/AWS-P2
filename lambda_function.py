import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get('TABLE_NAME', 'ChatApp')
USER_POOL_ID = os.environ.get('USER_POOL_ID', '')
CLIENT_ID = os.environ.get('CLIENT_ID', '')
COGNITO_REGION = os.environ.get('COGNITO_REGION') or os.environ.get('AWS_REGION', '')

MAX_TEXT = 1000
PAGE_SIZE = 30
MAX_ROOMS_PER_USER = 20
MESSAGE_TTL = 30 * 24 * 3600
CONNECTION_TTL = 3 * 3600
RANK = {'member': 1, 'admin': 2, 'owner': 3}
DIGEST_INFO = bytes.fromhex('3031300d060960864801650304020105000420')

table = boto3.resource('dynamodb').Table(TABLE_NAME)
workers = ThreadPoolExecutor(max_workers=8)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_keys = {}
_clients = {}


class ChatError(Exception):
    pass


class Session:
    def __init__(self, api, conn_id, user):
        self.api = api
        self.conn_id = conn_id
        self.user = user


# ---------- Cognito token check (no external packages) ----------

def issuer():
    return f'https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}'


def b64decode(part):
    return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))


def load_keys():
    with urllib.request.urlopen(f'{issuer()}/.well-known/jwks.json', timeout=5) as res:
        return {k['kid']: k for k in json.load(res)['keys']}


def username_from_token(token):
    try:
        head, body, sig = token.split('.')
        header = json.loads(b64decode(head))
        if header.get('alg') != 'RS256':
            return None
        if header['kid'] not in _keys:
            _keys.update(load_keys())
        key = _keys[header['kid']]
        n = int.from_bytes(b64decode(key['n']), 'big')
        e = int.from_bytes(b64decode(key['e']), 'big')
        size = (n.bit_length() + 7) // 8
        signed = pow(int.from_bytes(b64decode(sig), 'big'), e, n).to_bytes(size, 'big')
        digest = DIGEST_INFO + hashlib.sha256(f'{head}.{body}'.encode()).digest()
        expected = b'\x00\x01' + b'\xff' * (size - len(digest) - 3) + b'\x00' + digest
        if not hmac.compare_digest(signed, expected):
            return None
        claims = json.loads(b64decode(body))
        if claims['exp'] < time.time() or claims['iss'] != issuer():
            return None
        if claims.get('token_use') != 'id' or claims.get('aud') != CLIENT_ID:
            return None
        return claims['cognito:username'].lower()
    except Exception:
        return None


# ---------- DynamoDB and API Gateway helpers ----------

def get(pk, sk, consistent=False):
    return table.get_item(Key={'PK': pk, 'SK': sk}, ConsistentRead=consistent).get('Item')


def query_all(**kwargs):
    items = []
    while True:
        res = table.query(**kwargs)
        items += res['Items']
        if 'LastEvaluatedKey' not in res:
            return items
        kwargs['ExclusiveStartKey'] = res['LastEvaluatedKey']


def drop_connection(conn_id):
    table.delete_item(Key={'PK': f'CONN#{conn_id}', 'SK': 'META'})


def online_connections():
    now = int(time.time())
    items = query_all(IndexName='GSI1', KeyConditionExpression=Key('GSI1PK').eq('ONLINE'))
    return [{'id': i['GSI1SK'], 'user': i['username']} for i in items if i['expiresAt'] > now]


def api_client(ctx):
    url = f"https://{ctx['domainName']}/{ctx['stage']}"
    if url not in _clients:
        _clients[url] = boto3.client('apigatewaymanagementapi', endpoint_url=url)
    return _clients[url]


def push(api, targets, payload):
    data = json.dumps(payload, ensure_ascii=False).encode()

    def send_one(target):
        try:
            api.post_to_connection(ConnectionId=target['id'], Data=data)
        except api.exceptions.GoneException:
            return target['id']
        except ClientError:
            pass
        return None

    for conn_id in [c for c in workers.map(send_one, targets) if c]:
        drop_connection(conn_id)


def reply(s, payload):
    push(s.api, [{'id': s.conn_id, 'user': s.user}], payload)


def push_to_users(s, users, payload):
    users = set(users)
    push(s.api, [c for c in online_connections() if c['user'] in users], payload)


# ---------- chats ----------

def dm_id(a, b):
    return 'dm:' + ':'.join(sorted([a, b]))


def user_exists(name):
    return bool(name) and get(f'USER#{name}', 'PROFILE') is not None


def access(chat_id, user):
    if chat_id == 'public':
        return {'role': 'member', 'type': 'group'}
    kind, _, rest = chat_id.partition(':')
    if kind == 'room':
        member = get(f'ROOM#{rest}', f'MEMBER#{user}')
        if member:
            return {'role': member['role'], 'type': member['type']}
    elif kind == 'dm':
        parts = rest.split(':')
        if len(parts) == 2 and user in parts:
            return {'role': 'member', 'type': 'dm'}
    raise ChatError('به این گفتگو دسترسی ندارید')


def room_members(room_id, consistent=False):
    items = query_all(
        KeyConditionExpression=Key('PK').eq(f'ROOM#{room_id}') & Key('SK').begins_with('MEMBER#'),
        ConsistentRead=consistent,
    )
    return [i['username'] for i in items]


def audience(chat_id):
    conns = online_connections()
    if chat_id == 'public':
        return conns
    kind, _, rest = chat_id.partition(':')
    names = set(rest.split(':')) if kind == 'dm' else set(room_members(rest))
    return [c for c in conns if c['user'] in names]


def fmt(item):
    return {'id': item['SK'], 'sender': item['sender'], 'text': item['text'], 'ts': int(item['ts'])}


# ---------- rooms ----------

def member_item(room_id, room, user, role):
    return {
        'PK': f'ROOM#{room_id}', 'SK': f'MEMBER#{user}',
        'username': user, 'role': role, 'roomId': room_id,
        'name': room['name'], 'type': room['type'], 'joinedAt': int(time.time()),
        'GSI1PK': f'UROOM#{user}', 'GSI1SK': room_id,
    }


def rooms_of(user, include=None, exclude=None):
    items = query_all(IndexName='GSI1', KeyConditionExpression=Key('GSI1PK').eq(f'UROOM#{user}'))
    rooms = [
        {'id': i['roomId'], 'name': i['name'], 'type': i['type'], 'role': i['role']}
        for i in items if i['roomId'] != exclude
    ]
    if include:
        rooms = [r for r in rooms if r['id'] != include['id']] + [include]
    return rooms


def refresh_rooms(s, users, include=None, exclude=None):
    conns = online_connections()
    for user in set(users):
        targets = [c for c in conns if c['user'] == user]
        if targets:
            rooms = rooms_of(user, include, exclude)
            push(s.api, targets, {'type': 'rooms', 'rooms': rooms})


def notify_room(s, room_id, text):
    push_to_users(s, room_members(room_id), {'type': 'room_event', 'roomId': room_id, 'text': text})


def new_invite(room_id):
    code = secrets.token_urlsafe(6)
    table.put_item(Item={'PK': f'INVITE#{code}', 'SK': 'META', 'roomId': room_id})
    return code


def get_room(room_id):
    room = get(f'ROOM#{room_id}', 'META', consistent=True)
    if not room:
        raise ChatError('این گروه پیدا نشد')
    return room


def need_member(room_id, user):
    member = get(f'ROOM#{room_id}', f'MEMBER#{user}', consistent=True)
    if not member:
        raise ChatError('عضو پیدا نشد')
    return member


# ---------- actions ----------

def do_init(s, body):
    dms = query_all(KeyConditionExpression=Key('PK').eq(f'USER#{s.user}') & Key('SK').begins_with('DM#'))
    reply(s, {
        'type': 'init', 'me': s.user, 'rooms': rooms_of(s.user),
        'dms': [{'user': d['SK'][3:], 'last': int(d.get('lastTs', 0))} for d in dms],
    })


def do_history(s, body):
    chat_id = str(body.get('chatId') or '')
    before = str(body.get('before') or '')
    access(chat_id, s.user)
    cond = Key('PK').eq(f'CHAT#{chat_id}')
    if before:
        cond &= Key('SK').lt(before)
    items = table.query(KeyConditionExpression=cond, ScanIndexForward=False, Limit=PAGE_SIZE)['Items']
    reply(s, {
        'type': 'history', 'chatId': chat_id, 'before': bool(before),
        'messages': [fmt(i) for i in reversed(items)], 'hasMore': len(items) == PAGE_SIZE,
    })


def do_send(s, body):
    chat_id = str(body.get('chatId') or '')
    text = str(body.get('text') or '').strip()[:MAX_TEXT]
    if not text:
        return
    acc = access(chat_id, s.user)
    if acc['type'] == 'channel' and acc['role'] == 'member':
        raise ChatError('فقط مدیران کانال می‌توانند پیام بگذارند')
    peer = None
    if acc['type'] == 'dm':
        peer = next((p for p in chat_id.split(':')[1:] if p != s.user), None)
        if not user_exists(peer):
            raise ChatError('این کاربر پیدا نشد')
    ts = int(time.time() * 1000)
    sk = f'MSG#{ts:015d}#{secrets.token_hex(2)}'
    table.put_item(Item={
        'PK': f'CHAT#{chat_id}', 'SK': sk, 'sender': s.user, 'text': text,
        'ts': ts, 'expiresAt': ts // 1000 + MESSAGE_TTL,
    })
    if peer:
        table.put_item(Item={'PK': f'USER#{s.user}', 'SK': f'DM#{peer}', 'lastTs': ts})
        table.put_item(Item={'PK': f'USER#{peer}', 'SK': f'DM#{s.user}', 'lastTs': ts})
    push(s.api, audience(chat_id), {
        'type': 'message', 'chatId': chat_id, 'id': sk, 'sender': s.user, 'text': text, 'ts': ts,
    })


def do_search(s, body):
    q = str(body.get('q') or '').strip().lower().lstrip('@')[:20]
    online = {c['user'] for c in online_connections()}
    if q:
        names = [i['username'] for i in query_all(
            IndexName='GSI1', KeyConditionExpression=Key('GSI1PK').eq('USERS'))]
        names = sorted((n for n in names if q in n), key=lambda n: (not n.startswith(q), n))[:15]
    else:
        names = sorted(online)[:30]
    users = [{'name': n, 'online': n in online} for n in names if n != s.user]
    reply(s, {'type': 'users', 'q': q, 'users': users})


def do_open_dm(s, body):
    peer = str(body.get('user') or '').strip().lower()
    if peer == s.user:
        raise ChatError('نمی‌توانید به خودتان پیام بدهید')
    if not user_exists(peer):
        raise ChatError('این کاربر پیدا نشد')
    reply(s, {'type': 'dm_opened', 'chatId': dm_id(s.user, peer), 'user': peer})


def do_create_room(s, body):
    name = str(body.get('name') or '').strip()[:40]
    kind = 'channel' if body.get('type') == 'channel' else 'group'
    if len(name) < 2:
        raise ChatError('نام باید حداقل ۲ حرف باشد')
    if len(rooms_of(s.user)) >= MAX_ROOMS_PER_USER:
        raise ChatError('به سقف تعداد گروه‌ها رسیده‌اید')
    room_id = secrets.token_hex(4)
    room = {'name': name, 'type': kind}
    table.put_item(Item={
        'PK': f'ROOM#{room_id}', 'SK': 'META', 'name': name, 'type': kind,
        'owner': s.user, 'invite': new_invite(room_id), 'createdAt': int(time.time()),
    })
    table.put_item(Item=member_item(room_id, room, s.user, 'owner'))
    refresh_rooms(s, [s.user], include={'id': room_id, 'name': name, 'type': kind, 'role': 'owner'})
    reply(s, {'type': 'focus', 'chatId': f'room:{room_id}'})


def do_join_room(s, body):
    code = str(body.get('code') or '').strip()
    invite = get(f'INVITE#{code}', 'META') if code else None
    room = get(f"ROOM#{invite['roomId']}", 'META') if invite else None
    if not room:
        raise ChatError('لینک دعوت معتبر نیست')
    room_id = invite['roomId']
    member = get(f'ROOM#{room_id}', f'MEMBER#{s.user}', consistent=True)
    if not member:
        if len(rooms_of(s.user)) >= MAX_ROOMS_PER_USER:
            raise ChatError('به سقف تعداد گروه‌ها رسیده‌اید')
        table.put_item(Item=member_item(room_id, room, s.user, 'member'))
    role = member['role'] if member else 'member'
    refresh_rooms(s, [s.user], include={'id': room_id, 'name': room['name'], 'type': room['type'], 'role': role})
    reply(s, {'type': 'focus', 'chatId': f'room:{room_id}'})
    if not member:
        notify_room(s, room_id, f'{s.user} به گروه پیوست')


def do_room_info(s, body):
    room_id = str(body.get('roomId') or '')
    items = query_all(KeyConditionExpression=Key('PK').eq(f'ROOM#{room_id}'), ConsistentRead=True)
    room = next((i for i in items if i['SK'] == 'META'), None)
    members = [i for i in items if i['SK'].startswith('MEMBER#')]
    mine = next((m for m in members if m['username'] == s.user), None)
    if not room or not mine:
        raise ChatError('این گروه پیدا نشد')
    online = {c['user'] for c in online_connections()}
    members.sort(key=lambda m: (-RANK[m['role']], m['username']))
    reply(s, {
        'type': 'room_info', 'roomId': room_id, 'name': room['name'], 'kind': room['type'],
        'role': mine['role'], 'invite': room['invite'] if RANK[mine['role']] >= 2 else None,
        'members': [{'user': m['username'], 'role': m['role'], 'online': m['username'] in online} for m in members],
    })


def do_kick(s, body):
    room_id = str(body.get('roomId') or '')
    target = str(body.get('user') or '').lower()
    mine = need_member(room_id, s.user)
    theirs = need_member(room_id, target)
    if RANK[mine['role']] < 2 or RANK[mine['role']] <= RANK[theirs['role']]:
        raise ChatError('اجازه حذف این عضو را ندارید')
    table.delete_item(Key={'PK': f'ROOM#{room_id}', 'SK': f'MEMBER#{target}'})
    refresh_rooms(s, [target], exclude=room_id)
    push_to_users(s, [target], {'type': 'removed', 'roomId': room_id, 'text': 'شما از گروه حذف شدید'})
    notify_room(s, room_id, f'{target} از گروه حذف شد')


def do_set_role(s, body):
    room_id = str(body.get('roomId') or '')
    target = str(body.get('user') or '').lower()
    role = body.get('role')
    mine = need_member(room_id, s.user)
    theirs = need_member(room_id, target)
    if mine['role'] != 'owner' or theirs['role'] == 'owner' or role not in ('admin', 'member'):
        raise ChatError('اجازه تغییر نقش را ندارید')
    table.update_item(
        Key={'PK': f'ROOM#{room_id}', 'SK': f'MEMBER#{target}'},
        UpdateExpression='SET #r = :r',
        ExpressionAttributeNames={'#r': 'role'},
        ExpressionAttributeValues={':r': role},
    )
    refresh_rooms(s, [target], include={'id': room_id, 'name': theirs['name'], 'type': theirs['type'], 'role': role})
    notify_room(s, room_id, f'نقش {target} تغییر کرد')


def do_leave_room(s, body):
    room_id = str(body.get('roomId') or '')
    mine = need_member(room_id, s.user)
    if mine['role'] == 'owner':
        raise ChatError('مالک نمی‌تواند خارج شود؛ گروه را حذف کنید')
    table.delete_item(Key={'PK': f'ROOM#{room_id}', 'SK': f'MEMBER#{s.user}'})
    refresh_rooms(s, [s.user], exclude=room_id)
    notify_room(s, room_id, f'{s.user} از گروه خارج شد')


def do_rotate_invite(s, body):
    room_id = str(body.get('roomId') or '')
    mine = need_member(room_id, s.user)
    if RANK[mine['role']] < 2:
        raise ChatError('فقط مدیران می‌توانند لینک را عوض کنند')
    room = get_room(room_id)
    code = new_invite(room_id)
    table.update_item(
        Key={'PK': f'ROOM#{room_id}', 'SK': 'META'},
        UpdateExpression='SET invite = :c',
        ExpressionAttributeValues={':c': code},
    )
    table.delete_item(Key={'PK': f"INVITE#{room['invite']}", 'SK': 'META'})
    do_room_info(s, body)


def do_delete_room(s, body):
    room_id = str(body.get('roomId') or '')
    room = get_room(room_id)
    if room['owner'] != s.user:
        raise ChatError('فقط مالک می‌تواند گروه را حذف کند')
    items = query_all(KeyConditionExpression=Key('PK').eq(f'ROOM#{room_id}'), ConsistentRead=True)
    members = [i['username'] for i in items if i['SK'].startswith('MEMBER#')]
    messages = query_all(KeyConditionExpression=Key('PK').eq(f'CHAT#room:{room_id}'))
    with table.batch_writer() as batch:
        for item in items + messages:
            batch.delete_item(Key={'PK': item['PK'], 'SK': item['SK']})
        batch.delete_item(Key={'PK': f"INVITE#{room['invite']}", 'SK': 'META'})
    refresh_rooms(s, members, exclude=room_id)
    push_to_users(s, members, {'type': 'removed', 'roomId': room_id, 'text': f"گروه «{room['name']}» حذف شد"})


ACTIONS = {
    'init': do_init,
    'history': do_history,
    'send': do_send,
    'search_users': do_search,
    'open_dm': do_open_dm,
    'create_room': do_create_room,
    'join_room': do_join_room,
    'room_info': do_room_info,
    'kick': do_kick,
    'set_role': do_set_role,
    'leave_room': do_leave_room,
    'rotate_invite': do_rotate_invite,
    'delete_room': do_delete_room,
}


# ---------- routes ----------

def on_connect(event):
    ctx = event['requestContext']
    token = (event.get('queryStringParameters') or {}).get('token', '')
    user = username_from_token(token)
    if not user:
        return {'statusCode': 401}
    now = int(time.time())
    table.put_item(Item={
        'PK': f"CONN#{ctx['connectionId']}", 'SK': 'META', 'username': user,
        'GSI1PK': 'ONLINE', 'GSI1SK': ctx['connectionId'], 'expiresAt': now + CONNECTION_TTL,
    })
    if not get(f'USER#{user}', 'PROFILE'):
        table.put_item(Item={
            'PK': f'USER#{user}', 'SK': 'PROFILE', 'username': user,
            'createdAt': now, 'GSI1PK': 'USERS', 'GSI1SK': user,
        })
    return {'statusCode': 200}


def on_disconnect(event):
    drop_connection(event['requestContext']['connectionId'])
    return {'statusCode': 200}


def on_message(event):
    ctx = event['requestContext']
    try:
        body = json.loads(event.get('body') or '{}')
    except ValueError:
        return {'statusCode': 400}
    if not isinstance(body, dict):
        return {'statusCode': 400}
    action = body.get('action')
    if action == 'ping':
        return {'statusCode': 200}
    handler = ACTIONS.get(action)
    conn = get(f"CONN#{ctx['connectionId']}", 'META', consistent=True)
    if not handler or not conn:
        return {'statusCode': 400}
    s = Session(api_client(ctx), ctx['connectionId'], conn['username'])
    try:
        handler(s, body)
    except ChatError as err:
        reply(s, {'type': 'error', 'message': str(err)})
    return {'statusCode': 200}


def lambda_handler(event, context):
    if str(event.get('triggerSource', '')).startswith('PreSignUp'):
        event['response']['autoConfirmUser'] = True
        if 'email' in event['request'].get('userAttributes', {}):
            event['response']['autoVerifyEmail'] = True
        return event

    route = event['requestContext']['routeKey']
    try:
        if route == '$connect':
            return on_connect(event)
        if route == '$disconnect':
            return on_disconnect(event)
        return on_message(event)
    except Exception:
        logger.exception('unhandled error')
        return {'statusCode': 500}
