"""MagicOS 11 报告的本地服务：静态文件 + 文案写回接口。

    python3 scripts/serve.py [端口]        # 默认 5180；多个报告项目建议各自换端口

比 python -m http.server 多三件事：

  1. POST /__save  接收 {file, content, baseHash}，把报告写回磁盘。
     校验：请求体上限、路径必须落在项目根内、baseHash 不一致返回 409；
     写前自动备份到 _backup/<原文件名>.<YYYYmmdd-HHMMSS>.bak，并清理旧备份。
  2. GET 支持 Range 分段响应（206）——否则视频拖进度条会失效。
  3. HTML 响应带 no-store，避免保存后浏览器仍读到缓存里的旧版。

编辑模式的前端代码在报告里（见 assets/sample-report.html），服务端就是本文件，两者缺一不可。
"""
import datetime
import json
import os
import re
import shutil
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(ROOT, '_backup')
DRAFT_DIR = os.path.join(ROOT, '_draft')
KEEP_DRAFTS = 24
KEEP_BACKUPS = 40


def fnv1a_bytes(data):
    """与前端一致的 FNV-1a 32 位哈希（按 UTF-8 字节算）"""
    h = 0x811c9dc5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def prune_drafts():
    try:
        files = sorted(f for f in os.listdir(DRAFT_DIR) if f.endswith('.json'))
    except OSError:
        return
    for f in files[:-KEEP_DRAFTS]:
        try:
            os.remove(os.path.join(DRAFT_DIR, f))
        except OSError:
            pass


def prune_backups():
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith('.bak'))
    except OSError:
        return
    for f in files[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass


MEDIA_EXT = ('.mp4', '.m4v', '.m4a', '.mov', '.webm', '.ogg', '.mp3', '.wav')


class Handler(SimpleHTTPRequestHandler):
    server_version = 'ReportDevServer/1.0'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # ── 跨端口放行 ──
    # 报告页可能不是从本服务打开的（例如从编辑器/预览面板打开的快照副本），
    # 那样页面里的相对 __save 打不到这里，保存会失败。允许它跨端口 POST 回来。
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    # ── 统一 JSON 响应 ──
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # application/json 的跨域 POST 会先发预检，这里直接放行
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def end_headers(self):
        # 报告改完立刻能看到，别被缓存挡住
        if self.path.endswith(('.html', '.htm')) or self.path in ('/', ''):
            self.send_header('Cache-Control', 'no-store, must-revalidate')
        # 声明支持分段：视频要能拖进度条就得靠它
        if self.path.split('?')[0].lower().endswith(MEDIA_EXT):
            self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    # ── GET /__draft?file=<文件名>  取该文件最新的一份草稿 ──
    def _handle_draft_get(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        rel = (q.get('file') or [''])[0].strip()
        if not rel:
            return self._json(400, {'ok': False, 'error': 'file 缺失'})
        base = os.path.basename(rel)
        try:
            names = sorted(f for f in os.listdir(DRAFT_DIR)
                           if f.endswith('.json') and f.startswith(base + '.'))
        except OSError:
            names = []
        if not names:
            return self._json(200, {'ok': True, 'draft': None})
        # 只取最近 KEEP_DRAFTS 份里最新的一个：文件名带时间戳，字典序即时间序
        latest = names[-1]
        try:
            with open(os.path.join(DRAFT_DIR, latest), encoding='utf-8') as fh:
                body = json.load(fh)
        except Exception as exc:
            return self._json(500, {'ok': False, 'error': '读取草稿失败: %s' % exc})
        return self._json(200, {'ok': True, 'draft': latest, 'ts': body.get('ts'),
                                'map': body.get('map'), 'count': len(names)})

    def do_GET(self):
        if self.path.split('?', 1)[0] == '/__draft':
            return self._handle_draft_get()
        # python -m http.server 不支持 Range，视频拖进度条会失效（要么重头下载、要么干脆不动），
        # 这里自己实现 206 分段响应。
        if self.headers.get('Range') and self.path.split('?')[0].lower().endswith(MEDIA_EXT):
            path = self.translate_path(self.path)
            if os.path.isfile(path):
                return self._send_range(path, self.headers['Range'])
        return super().do_GET()

    def _send_range(self, path, rng):
        try:
            size = os.path.getsize(path)
            m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return super().do_GET()
            if not m.group(1):                      # bytes=-N  末尾 N 字节
                n = int(m.group(2)); start = max(0, size - n); end = size - 1
            else:
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            if start >= size or start > end:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Type', self.guess_type(path))
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            self.send_header('Content-Length', str(length))
            self.end_headers()
            with open(path, 'rb') as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ── 草稿：编辑过程中的自动备份，只存可编辑区域的内容映射，不碰正式文件 ──
    def _handle_draft_post(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > 8 * 1024 * 1024:
                return self._json(400, {'ok': False, 'error': '请求体大小异常'})
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception as exc:
            return self._json(400, {'ok': False, 'error': '解析请求失败: %s' % exc})

        rel = (payload.get('file') or '').strip()
        mapping = payload.get('map')

        # clear=true：保存成功后清掉该文件的全部草稿，否则老草稿会在下次打开时又弹恢复条
        if rel and payload.get('clear'):
            base = os.path.basename(rel)
            removed = 0
            try:
                for f in os.listdir(DRAFT_DIR):
                    if f.endswith('.json') and f.startswith(base + '.'):
                        os.remove(os.path.join(DRAFT_DIR, f)); removed += 1
            except OSError:
                pass
            print('[draft] 清除 %s 的草稿 %d 份' % (rel, removed))
            return self._json(200, {'ok': True, 'cleared': removed})

        if not rel or not isinstance(mapping, dict):
            return self._json(400, {'ok': False, 'error': 'file / map 缺失'})

        target = os.path.abspath(os.path.join(ROOT, rel))
        if not target.startswith(os.path.abspath(ROOT) + os.sep):
            return self._json(400, {'ok': False, 'error': '路径越界'})

        try:
            os.makedirs(DRAFT_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            name = os.path.basename(target) + '.' + stamp + '.json'
            body = {'file': rel, 'ts': int(datetime.datetime.now().timestamp() * 1000), 'map': mapping}
            with open(os.path.join(DRAFT_DIR, name), 'w', encoding='utf-8') as fh:
                json.dump(body, fh, ensure_ascii=False)
            prune_drafts()
        except Exception as exc:
            return self._json(500, {'ok': False, 'error': '草稿写入失败: %s' % exc})
        return self._json(200, {'ok': True, 'draft': name})

    def do_POST(self):
        ep = self.path.split('?', 1)[0]
        if ep == '/__draft':
            return self._handle_draft_post()
        if ep != '/__save':
            return self._json(404, {'ok': False, 'error': 'unknown endpoint'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > 20 * 1024 * 1024:
                return self._json(400, {'ok': False, 'error': '请求体大小异常'})
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception as exc:
            return self._json(400, {'ok': False, 'error': '解析请求失败: %s' % exc})

        rel = (payload.get('file') or '').strip()
        content = payload.get('content')
        base_hash = payload.get('baseHash')
        if not rel or not isinstance(content, str) or not content.strip():
            return self._json(400, {'ok': False, 'error': 'file / content 缺失'})

        target = os.path.abspath(os.path.join(ROOT, rel))
        if not target.startswith(os.path.abspath(ROOT) + os.sep):
            return self._json(400, {'ok': False, 'error': '路径越界'})
        if not os.path.isfile(target):
            return self._json(400, {'ok': False, 'error': '目标文件不存在: ' + rel})

        # 并发保护：前端进入编辑模式时记下源的哈希，保存时带回来比对。
        # 若磁盘上的文件已被外部改过（例如另一个进程/另一个人改了），直接拒绝，
        # 否则会把对方在编辑期间做的改动整段覆盖掉（真实事故：编辑期间外面的 CSS 更新被写没）。
        if base_hash is not None:
            try:
                base_hash = int(base_hash)
            except (TypeError, ValueError):
                base_hash = None
        if base_hash is not None:
            with open(target, 'rb') as fh:
                cur_hash = fnv1a_bytes(fh.read())
            if cur_hash != base_hash:
                print('[save] 拒绝写入 %s：磁盘版本已变（base=%s disk=%s）' % (rel, base_hash, cur_hash))
                return self._json(409, {'ok': False, 'stale': True,
                                        'error': '文件在编辑期间被外部改动过',
                                        'hash': cur_hash})

        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            backup = os.path.join(BACKUP_DIR, os.path.basename(target) + '.' + stamp + '.bak')
            shutil.copy2(target, backup)
            tmp = target + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(content)
            os.replace(tmp, target)
            prune_backups()
        except Exception as exc:
            return self._json(500, {'ok': False, 'error': '写入失败: %s' % exc})

        size = len(content.encode('utf-8'))
        print('[save] %s  ←  %d 字节   备份 %s' % (rel, size, os.path.basename(backup)))
        return self._json(200, {'ok': True, 'file': rel, 'bytes': size, 'backup': os.path.basename(backup)})


class QuietHandler(Handler):
    """浏览器中断请求（换页、seek、关闭标签）会抛 BrokenPipe / ConnectionReset，
    属正常现象，不用打整段堆栈把日志刷满。"""
    def handle_one_request(self):
        try:
            return super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_error(self, fmt, *args):
        msg = fmt % args if args else fmt
        if 'Broken pipe' in msg or 'Connection reset' in msg:
            return
        super().log_error(fmt, *args)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5180
    srv = ThreadingHTTPServer(('127.0.0.1', port), QuietHandler)
    print('报告服务: http://127.0.0.1:%d/   （Ctrl+C 停止）' % port)
    print('根目录: %s' % ROOT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
