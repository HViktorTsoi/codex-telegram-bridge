"""Snapshot explicitly returned attachments and deliver them to their saved route.

The historical class and sender names remain stable for existing queues.
"""
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit

from message_format import normalize_local_links

MAX_BYTES = 20 * 1024 * 1024
PHOTO_BYTES = 10 * 1024 * 1024
EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
DOCUMENT_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.odt', '.rtf', '.xls', '.xlsx', '.ods',
    '.ppt', '.pptx', '.odp', '.csv', '.tsv', '.txt', '.md', '.markdown',
    '.html', '.htm', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini',
    '.cfg', '.log', '.sql', '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
    '.cmd', '.bat', '.ps1', '.sh', '.bash', '.zsh', '.py', '.js', '.ts',
    '.jsx', '.tsx', '.css', '.scss', '.c', '.h', '.cpp', '.hpp', '.rs',
    '.go', '.java', '.kt', '.swift', '.rb', '.php', '.ipynb',
    '.mp3', '.m4a', '.wav', '.ogg', '.mp4', '.mov', '.webm',
}
SECRET_DIRECTORIES = {'.ssh', '.gnupg', '.aws', '.azure', '.kube', '.docker'}
SECRET_EXTENSIONS = {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.kdbx'}
SECRET_NAMES = {
    '.netrc', '.npmrc', '.pypirc', '.git-credentials', 'auth.json', 'tokens.json',
    'token.json', 'credentials.json', 'credential.json', 'application_default_credentials.json',
    'secret.json', 'secrets.json', 'secret.yaml', 'secrets.yaml', 'secret.yml', 'secrets.yml',
    'secrets.toml', 'secret.txt', 'secrets.txt', 'token.txt', 'tokens.txt',
}
DELIVERY = re.compile(
    r'(?:已(?:经)?|这次|直接|重新).{0,16}(?:附上|附了|发送|发出|发给|贴上).{0,16}(?:图片|截图|二维码|照片|图表)'
    r'|(?:图片|截图|二维码|照片|图表)\s*(?:如下|见下)'
    r'|(?:attached|here(?:\x27s| is| are)|sent).{0,30}(?:image|photo|screenshot|qr\s*code)', re.I)
NEGATIVE = re.compile(r'未|没(?:有)?|无法|不能|失败|尚未|\b(?:not|cannot|failed|unable)\b', re.I)


class ImageError(ConnectionError):
    uncertain = False


def local_image_path(destination):
    if destination.lower().startswith('file:'):
        url = urlsplit(destination)
        if url.netloc not in ('', 'localhost'):
            return None
        destination = url.path
    destination = unquote(destination)
    if not (destination.startswith('/') or destination.startswith('~/')):
        return None
    path = Path(destination).expanduser()
    return path if path.suffix.lower() in EXTENSIONS else None


def credential_path(path):
    """Never automatically attach obvious credential stores or their aliases."""
    parts = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    pairs = set(zip(parts, parts[1:]))
    return (any(part in SECRET_DIRECTORIES for part in parts)
            or any(pair in pairs for pair in (('.claude', 'channels'), ('.codex', 'channels'),
                                             ('.config', 'gcloud'), ('.config', 'gh')))
            or name in SECRET_NAMES or name == '.env' or name.startswith('.env.')
            or path.suffix.casefold() in SECRET_EXTENSIONS
            or name.startswith(('id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa',
                                'private_key', 'private-key', 'secret_key', 'secret-key',
                                'client_secret', 'client-secret', 'service_account',
                                'service-account', 'credentials.', 'credentials-')))


def local_document_path(destination):
    if destination.lower().startswith('file:'):
        url = urlsplit(destination)
        if url.netloc not in ('', 'localhost') or url.query or url.fragment:
            return None
        destination = url.path
    destination = unquote(destination)
    if (not destination.startswith(('/', '~/'))
            or re.search(r':\d+(?::\d+)?$', destination)):
        return None
    path = Path(destination).expanduser()
    return path if path.suffix.lower() in DOCUMENT_EXTENSIONS and not credential_path(path) else None


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    raise ImageError('Unsupported image content')


class OutputImages:
    def __init__(self, store, directory):
        self.store, self.db = store, store.db
        self.directory = Path(directory) / 'outgoing-images'
        self.files_directory = Path(directory) / 'outgoing-files'
        self.db.execute('''CREATE TABLE IF NOT EXISTS output_image_views(
            turn_id TEXT,item_id TEXT,path TEXT,completed_ms INTEGER,
            PRIMARY KEY(turn_id,item_id))''')
        self.db.commit()

    def observe(self, turn_id, item, completed_ms=None):
        if item.get('type') == 'imageView' and isinstance(item.get('path'), str):
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO output_image_views VALUES(?,?,?,?)',
                                (turn_id, item['id'], item['path'], completed_ms or item.get('completedAtMs') or 0))

    def agent_text(self, turn_id, item):
        text = item.get('text', '')
        if item.get('phase') not in (None, 'final_answer'):
            return text
        # Only a final, explicit delivery claim exposes the last viewed image.
        # Viewing screenshots for analysis alone is never a delivery instruction.
        if not any(DELIVERY.search(line) and not NEGATIVE.search(line) for line in text.splitlines()):
            return text
        explicit = []
        normalize_local_links(text, rewrite=lambda label, dest, image: explicit.append(dest) if local_image_path(dest) else None)
        if explicit:
            return text
        row = self.db.execute('SELECT path FROM output_image_views WHERE turn_id=? '
                              'ORDER BY completed_ms DESC,rowid DESC LIMIT 1', (turn_id,)).fetchone()
        if row and local_image_path(row['path']):
            return text + '\n\n![图片](<' + row['path'].replace('>', '%3E') + '>)'
        return text

    def snapshot(self, path, label):
        try:
            resolved = path.resolve(strict=True)
            fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ImageError('Image must be a regular file no larger than 20 MB')
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ImageError('Image exceeds 20 MB')
            mime, extension = image_type(data)
        except (OSError, RuntimeError, ValueError) as error:
            raise ImageError('Image file is unavailable') from None
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.directory / (digest + extension)
            if not target.exists():
                fd, tmp = tempfile.mkstemp(prefix='.snapshot-', dir=self.directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmp, target)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
        except OSError:
            raise ImageError('Image snapshot cannot be saved') from None
        return {'path': str(target), 'sha256': digest, 'name': path.name[:150],
                'caption': ' '.join(label.split())[:200], 'mime': mime, 'bytes': len(data),
                'method': 'sendDocument' if len(data) > PHOTO_BYTES or mime == 'image/gif' else 'sendPhoto',
                'delivery': 'queued'}

    def snapshot_document(self, path, label):
        try:
            if credential_path(path):
                raise ImageError('Document path is excluded')
            resolved = path.resolve(strict=True)
            if credential_path(resolved) or resolved.suffix.lower() not in DOCUMENT_EXTENSIONS:
                raise ImageError('Document path is excluded')
            fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                    raise ImageError('Document must be a nonempty regular file no larger than 20 MB')
                data = stream.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES:
                raise ImageError('Document is empty or exceeds 20 MB')
        except (OSError, RuntimeError, ValueError):
            raise ImageError('Document file is unavailable') from None
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.files_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.files_directory / (digest + path.suffix.lower())
            if not target.exists():
                fd, tmp = tempfile.mkstemp(prefix='.snapshot-', dir=self.files_directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmp, target)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
        except OSError:
            raise ImageError('Document snapshot cannot be saved') from None
        mime, encoding = mimetypes.guess_type(path.name)
        mime = {'gzip': 'application/gzip', 'bzip2': 'application/x-bzip2',
                'xz': 'application/x-xz'}.get(encoding, mime)
        return {'path': str(target), 'sha256': digest, 'name': path.name,
                'caption': ' '.join(label.split())[:200], 'mime': mime or 'application/octet-stream',
                'bytes': len(data), 'method': 'sendDocument', 'delivery': 'queued', 'kind': 'document'}

    def prepare(self, text):
        images, seen = [], set()
        changed = False

        def capture(label, destination, image):
            nonlocal changed
            path = local_image_path(destination)
            document = False
            if path is None and not image:
                path = local_document_path(destination)
                document = path is not None
            if path is None:
                return None
            changed = True
            try:
                media = self.snapshot_document(path, label) if document else self.snapshot(path, label)
            except ImageError:
                kind = '文件' if document else '图片'
                return f'［{kind}未发送：{label}，文件不可读取、格式不支持或超过 20 MB］'
            identity = ('document', media['sha256'], media['name']) if document else ('image', media['sha256'])
            if identity not in seen:
                images.append(media)
                seen.add(identity)
            return ''

        result = normalize_local_links(text, rewrite=capture)
        return (result.strip() if changed else text), images

    def begin(self, row):
        if row['media_json']:
            media = json.loads(row['media_json'])
            media['delivery'] = 'sending'
            with self.db:
                self.db.execute('UPDATE outbox SET media_json=? WHERE key=?', (json.dumps(media), row['key']))

    def retry(self, row):
        if row['media_json']:
            media = json.loads(row['media_json'])
            media['delivery'] = 'queued'
            with self.db:
                self.db.execute('UPDATE outbox SET media_json=? WHERE key=?', (json.dumps(media), row['key']))

    def failed(self, row, uncertain=False):
        if not row['media_json']:
            return
        media = json.loads(row['media_json'])
        media['delivery'] = 'uncertain' if uncertain else 'failed'
        with self.db:
            self.db.execute('UPDATE outbox SET message_id=-1,media_json=? WHERE key=?',
                            (json.dumps(media), row['key']))
        kind = '文件' if media.get('kind') == 'document' else '图片'
        notice = (f'{kind}发送结果未获确认，未自动重发；请明确要求重新发送{kind}。' if uncertain
                  else f'{kind}未能发送，请重新生成或要求重新发送{kind}。')
        if hasattr(self.store, 'enqueue_at'):
            self.store.enqueue_at('image-error:' + row['key'], row['chat_id'], notice, row['message_thread_id'])
        else:
            self.store.enqueue('image-error:' + row['key'], row['chat_id'], notice)

    def sent(self, row, message_id):
        media_json = row['media_json']
        if media_json:
            media = json.loads(media_json)
            media['delivery'] = 'sent'
            media_json = json.dumps(media)
        with self.db:
            self.db.execute('UPDATE outbox SET message_id=?,media_json=? WHERE key=?',
                            (message_id, media_json, row['key']))

    def recover(self):
        # Only the sender's startup calls this, never a read-only Store constructor.
        for row in self.db.execute('SELECT * FROM outbox WHERE media_json IS NOT NULL AND message_id IS NULL').fetchall():
            if json.loads(row['media_json']).get('delivery') == 'sending':
                self.failed(row, uncertain=True)


async def send_image(telegram, media_json, route):
    media = json.loads(media_json)
    kind, method = media.get('kind', 'image'), media.get('method')
    label = 'Document' if kind == 'document' else 'Image'
    if (kind not in ('image', 'document') or method not in ('sendPhoto', 'sendDocument')
            or (kind == 'document' and method != 'sendDocument')):
        raise ImageError('Unsupported attachment method')
    try:
        path = Path(media['path'])
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                raise ImageError(f'{label} snapshot is unavailable')
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != media['sha256']:
            raise ImageError(f'{label} snapshot changed')
    except OSError:
        raise ImageError(f'{label} snapshot is unavailable') from None
    params = {**route, 'caption': media['caption']}
    try:
        return await telegram.upload(method, 'photo' if method == 'sendPhoto' else 'document', path,
                                     filename=media['name'], mime_type=media['mime'], **params)
    except Exception as error:
        if isinstance(error, OSError) and not isinstance(error, ConnectionError):
            raise ImageError(f'{label} snapshot is unavailable') from None
        # Fallback only on an explicit photo-format rejection, never on lost
        # responses, missing topics, permissions, throttling or server errors.
        if (method != 'sendPhoto' or getattr(error, 'code', None) != 400
                or not any(word in str(error).lower() for word in
                           ('photo_invalid_dimensions', 'image_process_failed', 'photo_content_type_invalid',
                            'photo_ext_invalid', 'photo_invalid'))):
            raise
        return await telegram.upload('sendDocument', 'document', path,
                                     filename=media['name'], mime_type=media['mime'], **params)
