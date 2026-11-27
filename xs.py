#!/usr/bin/env python3
"""xs - compact xschem schematic/symbol reader, editor and checker.

Pure-python (stdlib only) for read/edit/check; xschem (headless, inside the
distrobox container) only for netlist / render / xcheck.
Run `xs.py help` for usage.
"""
import sys, os, re, json, math, subprocess, shlex, hashlib, fnmatch, time

HOME = os.path.expanduser('~')
CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.join(HOME, '.cache')), 'xschem-skill')
MIRROR = os.path.join(CACHE, 'mirror')
CONTAINER = os.environ.get('XS_CONTAINER', 'iic-osic-tools2')
INSIDE = os.path.isdir('/foss/tools/xschem')
DEFAULT_VERSION = 'xschem version=3.4.8RC file_version=1.3'
LABEL_TYPES = ('label', 'ipin', 'opin', 'iopin')
PORT_TYPES = ('ipin', 'opin', 'iopin')
NOISE_PROPS = {'name', 'spiceprefix', 'sig_type', 'mm_ok', 'savecurrent', 'verilog_type', 'only_toplevel'}
EPS = 1e-6
ARROW = {'l': '<', 'r': '>', 'u': '^', 'd': 'v'}
DIRV = {'l': (-1, 0), 'r': (1, 0), 'u': (0, -1), 'd': (0, 1)}


class XsError(Exception):
    pass


# ----------------------------------------------------------------- utilities
def fmt(v):
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return ('%.6f' % v).rstrip('0').rstrip('.')


def fpt(p):
    return '%s,%s' % (fmt(p[0]), fmt(p[1]))


def xform(px, py, rot, flip):
    """xschem ROTATION macro around origin (y axis points down)."""
    if flip:
        px = -px
    rot &= 3
    if rot == 0:
        return px, py
    if rot == 1:
        return -py, px
    if rot == 2:
        return -px, -py
    return py, -px


def dir_of_vec(dx, dy):
    if abs(dx) >= abs(dy):
        return 'r' if dx > 0 else 'l'
    return 'd' if dy > 0 else 'u'


def xform_dir(d, rot, flip):
    v = xform(DIRV[d][0], DIRV[d][1], rot, flip)
    return dir_of_vec(*v)


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def tilde(p):
    return '~' + p[len(HOME):] if p and p.startswith(HOME + '/') else p


# ------------------------------------------------------------------ escaping
def file_unescape(s):
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n and s[i + 1] in '\\{}':
            out.append(s[i + 1]); i += 2
        else:
            out.append(c); i += 1
    return ''.join(out)


def file_escape(s):
    return s.replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}')


def prop_spans(s):
    """Parse xschem property string -> list of (key, value, start, end)."""
    res, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        st = i
        j = i
        while j < n and not s[j].isspace() and s[j] != '=':
            j += 1
        key = s[i:j]
        if j >= n or s[j] != '=':
            i = j
            continue
        j += 1
        buf = []
        if j < n and s[j] == '"':
            j += 1
            while j < n and s[j] != '"':
                if s[j] == '\\' and j + 1 < n:
                    buf.append(s[j + 1]); j += 2
                else:
                    buf.append(s[j]); j += 1
            j += 1
        else:
            while j < n and not s[j].isspace():
                if s[j] == '\\' and j + 1 < n:
                    buf.append(s[j + 1]); j += 2
                else:
                    buf.append(s[j]); j += 1
        res.append((key, ''.join(buf), st, min(j, n)))
        i = j
    return res


def parse_props(s):
    d = {}
    for k, v, _, _ in prop_spans(s):
        d[k] = v
    return d


def quote_val(v):
    if v == '' or any(c.isspace() or c in '"\\' for c in v):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return v


def set_prop(s, key, value):
    """Return prop string with key set (value None deletes)."""
    for k, v, st, en in prop_spans(s):
        if k == key:
            if value is None:
                return (s[:st] + s[en:]).strip('\n ') if True else s
            return s[:st] + key + '=' + quote_val(value) + s[en:]
    if value is None:
        return s
    sep = '\n' if '\n' in s else ' '
    s2 = s.rstrip()
    return (s2 + sep if s2 else '') + key + '=' + quote_val(value)


def props_from_dict(d):
    return '\n'.join('%s=%s' % (k, quote_val(v)) for k, v in d.items())


# -------------------------------------------------------------------- parser
class Obj:
    __slots__ = ('kind', 'f', 'raw', 'dirty', 'emb')

    def __init__(self, kind, f, raw=None):
        self.kind, self.f, self.raw, self.dirty, self.emb = kind, f, raw, False, None

    # props live in f['p'] as memory (unescaped) string
    @property
    def props(self):
        return parse_props(self.f.get('p', ''))

    def setp(self, key, value):
        self.f['p'] = set_prop(self.f.get('p', ''), key, value)
        self.dirty = True

    def text(self):
        if self.raw is not None and not self.dirty:
            return self.raw
        f, k = self.f, self.kind
        P = '{' + file_escape(f.get('p', '')) + '}'
        if k in 'vGKVSFE':
            s = '%s {%s}' % (k, file_escape(f['t']))
        elif k in 'LB':
            s = '%s %d %s %s %s %s %s' % (k, f['layer'], fmt(f['x1']), fmt(f['y1']), fmt(f['x2']), fmt(f['y2']), P)
        elif k == 'N':
            s = 'N %s %s %s %s %s' % (fmt(f['x1']), fmt(f['y1']), fmt(f['x2']), fmt(f['y2']), P)
        elif k == 'A':
            s = 'A %d %s %s %s %s %s %s' % (f['layer'], fmt(f['x']), fmt(f['y']), fmt(f['r']), fmt(f['a']), fmt(f['b']), P)
        elif k == 'P':
            s = 'P %d %d %s %s' % (f['layer'], len(f['pts']), ' '.join('%s %s' % (fmt(x), fmt(y)) for x, y in f['pts']), P)
        elif k == 'T':
            s = 'T {%s} %s %s %d %d %s %s %s' % (file_escape(f['t']), fmt(f['x']), fmt(f['y']), f['rot'], f['flip'], fmt(f['xs']), fmt(f['ys']), P)
        elif k == 'C':
            s = 'C {%s} %s %s %d %d %s' % (file_escape(f['sym']), fmt(f['x']), fmt(f['y']), f['rot'], f['flip'], P)
            if self.emb is not None:
                s += '\n' + self.emb
        else:
            s = f.get('t', '')
        return s


class Parser:
    def __init__(self, text):
        self.s, self.i, self.n = text, 0, len(text)

    def ws(self):
        s, i, n = self.s, self.i, self.n
        while i < n and s[i] in ' \t\r\n':
            i += 1
        self.i = i

    def brace(self):
        self.ws()
        s, i = self.s, self.i
        if i >= self.n or s[i] != '{':
            raise XsError('parse error at offset %d: expected {' % i)
        depth, j = 1, i + 1
        while j < self.n:
            c = s[j]
            if c == '\\':
                j += 2
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        self.i = j + 1
        return file_unescape(s[i + 1:j])

    def num(self):
        self.ws()
        s, i = self.s, self.i
        j = i
        while j < self.n and s[j] not in ' \t\r\n{':
            j += 1
        self.i = j
        try:
            return float(s[i:j])
        except ValueError:
            raise XsError('parse error: bad number %r at offset %d' % (s[i:j], i))

    def parse(self, stop=None):
        objs = []
        s = self.s
        while True:
            self.ws()
            if self.i >= self.n:
                break
            st = self.i
            c = s[st]
            if stop and c == stop:
                self.i += 1
                break
            self.i += 1
            if c in 'vGKVSFE':
                o = Obj(c, {'t': self.brace()})
            elif c in 'LB':
                o = Obj(c, {'layer': int(self.num()), 'x1': self.num(), 'y1': self.num(), 'x2': self.num(), 'y2': self.num(), 'p': self.brace()})
            elif c == 'N':
                o = Obj(c, {'x1': self.num(), 'y1': self.num(), 'x2': self.num(), 'y2': self.num(), 'p': self.brace()})
            elif c == 'A':
                o = Obj(c, {'layer': int(self.num()), 'x': self.num(), 'y': self.num(), 'r': self.num(), 'a': self.num(), 'b': self.num(), 'p': self.brace()})
            elif c == 'P':
                layer = int(self.num()); npt = int(self.num())
                pts = [(self.num(), self.num()) for _ in range(npt)]
                o = Obj(c, {'layer': layer, 'pts': pts, 'p': self.brace()})
            elif c == 'T':
                t = self.brace()
                o = Obj(c, {'t': t, 'x': self.num(), 'y': self.num(), 'rot': int(self.num()), 'flip': int(self.num()),
                            'xs': self.num(), 'ys': self.num(), 'p': self.brace()})
            elif c == 'C':
                sym = self.brace()
                o = Obj(c, {'sym': sym, 'x': self.num(), 'y': self.num(), 'rot': int(self.num()), 'flip': int(self.num()), 'p': self.brace()})
                o.raw = s[st:self.i]
                self.ws()
                if self.i < self.n and s[self.i] == '[':
                    est = self.i
                    self.i += 1
                    sub = self.parse(stop=']')
                    o.emb = s[est:self.i]
                    o.f['embobjs'] = sub
                objs.append(o)
                continue
            else:  # unknown line: keep verbatim
                j = s.find('\n', st)
                j = self.n if j < 0 else j
                self.i = j
                o = Obj('?', {'t': s[st:j]})
            o.raw = s[st:self.i]
            objs.append(o)
        return objs


def parse_text(text):
    return Parser(text).parse()


# --------------------------------------------------------------- environment
def run_container(script, cwd, timeout=180):
    full = 'cd %s && %s' % (shlex.quote(cwd), script)
    cmd = ['sh', '-c', full] if INSIDE else ['distrobox', 'enter', CONTAINER, '--', 'sh', '-c', full]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise XsError('container command timed out')
    out = '\n'.join(l for l in p.stdout.splitlines() if not l.startswith('[INFO]'))
    err = '\n'.join(l for l in p.stderr.splitlines() if not l.startswith('[INFO]'))
    return p.returncode, out, err


def find_rc(start):
    ov = os.environ.get('XS_RC')
    if ov:
        return os.path.abspath(ov) if ov != 'default' else None
    d = os.path.abspath(start)
    while True:
        f = os.path.join(d, 'xschemrc')
        if os.path.isfile(f):
            return f
        p = os.path.dirname(d)
        if p == d:
            break
        d = p
    f = os.path.join(os.getcwd(), 'xschemrc')
    return f if os.path.isfile(f) else None


def xschem_cmd(rc, args):
    rco = '--rcfile %s ' % shlex.quote(rc) if rc else ''
    return 'timeout 120 xschem -q -x -r %s%s </dev/null' % (rco, args)


class Env:
    def __init__(self, start_dir, refresh=False):
        self.rc = find_rc(start_dir)
        self.cwd = os.path.dirname(self.rc) if self.rc else os.path.abspath(start_dir)
        rcf = self.rc or os.path.join(HOME, '.xschem', 'xschemrc')
        mt = os.path.getmtime(rcf) if os.path.exists(rcf) else 0
        key = hashlib.sha1((self.rc or 'default').encode()).hexdigest()[:12]
        cf = os.path.join(CACHE, 'env-%s.json' % key)
        data = None
        if not refresh and os.path.exists(cf):
            try:
                data = json.load(open(cf))
                if data.get('mtime') != mt or data.get('rc') != self.rc:
                    data = None
            except Exception:
                data = None
        if data is None:
            data = self._query(mt)
            os.makedirs(CACHE, exist_ok=True)
            json.dump(data, open(cf, 'w'), indent=1)
        self.pathlist = data['pathlist']
        self.version = data.get('version') or DEFAULT_VERSION
        self._ensure_mirror(refresh)
        self._symcache = {}
        self._resolve_cache = {}
        self.fallback = set()

    def _query(self, mt):
        tcl = 'foreach p $pathlist {puts "XSP:$p"}; catch {puts "XSV:[xschem get version]"}; exit'
        rc, out, err = run_container(xschem_cmd(self.rc, '--command %s' % shlex.quote(tcl)), self.cwd)
        pl = [l[4:] for l in out.splitlines() if l.startswith('XSP:')]
        if not pl:
            raise XsError('could not query xschem pathlist (rc=%s):\n%s\n%s' % (self.rc, out[-800:], err[-800:]))
        seen, pl2 = set(), []
        for p in pl:
            if p not in seen:
                seen.add(p); pl2.append(p)
        ver = None
        for l in out.splitlines():
            if l.startswith('XSV:'):
                v = l[4:].strip()
                m = re.search(r'(\d+\.\d+\.\d+\w*)', v)
                if m:
                    ver = 'xschem version=%s file_version=1.3' % m.group(1)
        return {'rc': self.rc, 'mtime': mt, 'pathlist': pl2, 'version': ver, 'time': time.time()}

    def _ensure_mirror(self, refresh):
        if INSIDE:
            return
        idx = os.path.join(MIRROR, '.dirs.json')
        done = []
        if os.path.exists(idx) and not refresh:
            try:
                done = json.load(open(idx))
            except Exception:
                done = []
        missing = [d for d in self.pathlist if not os.path.isdir(d) and d not in done and d.startswith('/')]
        if not missing:
            return
        os.makedirs(MIRROR, exist_ok=True)
        tarf = os.path.join(CACHE, 'mirror.tar')
        script = 'cd / && find %s \\( -name "*.sym" -o -name "*.tcl" \\) 2>/dev/null | tar -cf %s -T - 2>/dev/null; echo done' % (
            ' '.join(shlex.quote(d) for d in missing), shlex.quote(tarf))
        run_container(script, '/')
        if os.path.exists(tarf):
            subprocess.run(['tar', '-xf', tarf, '-C', MIRROR], check=False)
            os.remove(tarf)
        json.dump(sorted(set(done + missing)), open(idx, 'w'))

    @staticmethod
    def hostpath(p):
        if os.path.exists(p):
            return p
        m = MIRROR + p
        return m if os.path.exists(m) else None

    def resolve(self, ref, schdir=None):
        """symbol reference -> (abs path as xschem sees it, host-readable path) or (None, None)."""
        key = (ref, schdir)
        if key in self._resolve_cache:
            return self._resolve_cache[key]
        cands = [ref] if ref.endswith('.sym') or ref.endswith('.sch') else [ref, ref + '.sym']
        res = (None, None)
        for r in cands:
            if os.path.isabs(r):
                hp = self.hostpath(r)
                if hp:
                    res = (r, hp)
                    break
                continue
            for d in self.pathlist:
                p = os.path.normpath(os.path.join(d, r))
                hp = self.hostpath(p)
                if hp and os.path.isfile(hp):
                    res = (p, hp)
                    break
            if res[0]:
                break
        if not res[0] and schdir and not os.path.isabs(ref):
            # xschem itself does NOT search the schematic dir; remember as fallback
            for r in cands:
                p = os.path.normpath(os.path.join(schdir, r))
                if os.path.isfile(p):
                    res = (p, p)
                    self.fallback.add(ref)
                    break
        self._resolve_cache[key] = res
        return res

    def short_ref(self, abspath, schdir=None):
        """shortest valid reference; prefers 'lib/cell.sym' (e.g. sg13g2_pr/...) over a bare name,
        except for xschem's devices library (convention: bare 'res.sym')."""
        cands = []
        for d in self.pathlist + ([schdir] if schdir else []):
            if abspath.startswith(d.rstrip('/') + '/'):
                rel = abspath[len(d.rstrip('/')) + 1:]
                if rel not in cands and self.resolve(rel, schdir)[0] == abspath:
                    cands.append(rel)
        if not cands:
            return abspath
        lib = [c for c in cands if c.count('/') == 1 and not c.startswith('devices/')]
        return min(lib or cands, key=len)

    def sym(self, ref, schdir=None):
        ap, hp = self.resolve(ref, schdir)
        if not hp:
            return None
        if hp not in self._symcache:
            self._symcache[hp] = Sym(parse_text(open(hp, errors='replace').read()), ap, hp)
        return self._symcache[hp]

    def all_syms(self):
        seen = {}
        for d in self.pathlist:
            hd = self.hostpath(d)
            if not hd or not os.path.isdir(hd):
                continue
            for root, dirs, files in os.walk(hd):
                if root[len(hd):].count(os.sep) >= 2:
                    dirs[:] = []
                for f in files:
                    if f.endswith('.sym'):
                        hp = os.path.join(root, f)
                        ap = hp[len(MIRROR):] if hp.startswith(MIRROR + '/') else hp
                        if ap not in seen:
                            seen[ap] = hp
        return seen

    def find_sym(self, name):
        """Find a symbol by loose name (basename w/o .sym). Returns list of short refs."""
        base = os.path.basename(name)
        if not base.endswith('.sym'):
            base += '.sym'
        out = []
        for ap in self.all_syms():
            if os.path.basename(ap) == base:
                r = self.short_ref(ap)
                if r not in out:
                    out.append(r)
        return out


# -------------------------------------------------------------------- symbol
def text_box(t):
    """approximate box of a T primitive -> (x1,y1,x2,y2)"""
    lines = t['t'].split('\n')
    w = max(len(l) for l in lines) * t['xs'] * 30
    h = len(lines) * t['ys'] * 50
    dx, dy = xform(w, h, t['rot'], t['flip'])
    return (min(t['x'], t['x'] + dx), min(t['y'], t['y'] + dy), max(t['x'], t['x'] + dx), max(t['y'], t['y'] + dy))


class Sym:
    def __init__(self, objs, ap, hp):
        self.objs, self.path, self.hostpath = objs, ap, hp
        K = next((o for o in objs if o.kind == 'K'), None)
        G = next((o for o in objs if o.kind == 'G'), None)
        kt = K.f['t'] if K and K.f['t'].strip() else (G.f['t'] if G else '')
        self.kprops = parse_props(kt)
        self.type = self.kprops.get('type', '')
        self.format = self.kprops.get('format', '')
        self.template_str = self.kprops.get('template', '')
        self.template = parse_props(self.template_str)
        self.pins = []
        body = []
        textb = []
        labtext = []
        self.lines = []
        for o in objs:
            f = o.f
            if o.kind == 'B' and f['layer'] == 5:
                p = o.props
                self.pins.append({'name': p.get('name', '?'), 'dir': p.get('dir', 'inout'),
                                  'x': (f['x1'] + f['x2']) / 2, 'y': (f['y1'] + f['y2']) / 2, 'props': p})
            elif o.kind in 'LB':
                body.append((min(f['x1'], f['x2']), min(f['y1'], f['y2']), max(f['x1'], f['x2']), max(f['y1'], f['y2'])))
                if o.kind == 'L':
                    self.lines.append((f['x1'], f['y1'], f['x2'], f['y2']))
            elif o.kind == 'P' and f['pts']:
                xs = [p[0] for p in f['pts']]; ys = [p[1] for p in f['pts']]
                body.append((min(xs), min(ys), max(xs), max(ys)))
                for a, b in zip(f['pts'], f['pts'][1:]):
                    self.lines.append((a[0], a[1], b[0], b[1]))
            elif o.kind == 'A':
                body.append((f['x'] - f['r'], f['y'] - f['r'], f['x'] + f['r'], f['y'] + f['r']))
            elif o.kind == 'T':
                textb.append(text_box(f))
                if '@lab' in f['t'] or ('spice_get' not in f['t'] and 'layer=15' not in f.get('p', '')):
                    labtext.append(text_box(f))
        self.body = self._union(body)
        self.bbox = self._union(body + textb + [(p['x'], p['y'], p['x'], p['y']) for p in self.pins])
        self._gfx = body + labtext

    @staticmethod
    def _union(boxes):
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def pin(self, name):
        for p in self.pins:
            if p['name'] == name:
                return p
        for p in self.pins:
            if p['name'].lower() == name.lower():
                return p
        return None

    def pin_out(self, p):
        """outward direction of pin in symbol-local coordinates."""
        x, y = p['x'], p['y']
        for (x1, y1, x2, y2) in self.lines:
            for (ax, ay, bx, by) in ((x1, y1, x2, y2), (x2, y2, x1, y1)):
                if abs(ax - x) < 0.01 and abs(ay - y) < 0.01 and (abs(bx - ax) > 0.01 or abs(by - ay) > 0.01):
                    if abs(bx - ax) < 0.01 or abs(by - ay) < 0.01:
                        return dir_of_vec(ax - bx, ay - by)
        b = self.body or self.bbox
        if not b:
            return 'l'
        d = {'l': x - b[0], 'r': b[2] - x, 'u': y - b[1], 'd': b[3] - y}
        return min('lrud', key=lambda k: d[k])

    def body_dir(self):
        """direction from (single) pin towards the symbol graphics (labels)."""
        if not self._gfx:
            return 'l'
        px, py = (self.pins[0]['x'], self.pins[0]['y']) if self.pins else (0, 0)
        cx = sum((b[0] + b[2]) / 2 for b in self._gfx) / len(self._gfx)
        cy = sum((b[1] + b[3]) / 2 for b in self._gfx) / len(self._gfx)
        return dir_of_vec(cx - px, cy - py)


# ----------------------------------------------------------------- schematic
def on_seg(px, py, x1, y1, x2, y2, tol=1e-3):
    if px < min(x1, x2) - tol or px > max(x1, x2) + tol or py < min(y1, y2) - tol or py > max(y1, y2) + tol:
        return False
    if abs(x1 - x2) < tol or abs(y1 - y2) < tol:
        return True
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    return abs(cross) / math.hypot(x2 - x1, y2 - y1) < tol


class Inst:
    """view over a C object"""

    def __init__(self, o, sch):
        self.o, self.sch = o, sch

    name = property(lambda s: s.o.props.get('name', '?'))
    ref = property(lambda s: s.o.f['sym'])
    x = property(lambda s: s.o.f['x'])
    y = property(lambda s: s.o.f['y'])
    rot = property(lambda s: s.o.f['rot'])
    flip = property(lambda s: s.o.f['flip'])

    @property
    def sym(self):
        emb = self.o.f.get('embobjs')
        if emb is not None and self.o.props.get('embed') == 'true':
            return Sym(emb, self.ref, None)
        return self.sch.env.sym(self.ref, self.sch.dir) if self.sch.env else None

    def prop(self, k):
        p = self.o.props
        if k in p:
            return p[k]
        s = self.sym
        return s.template.get(k) if s else None

    @property
    def type(self):
        s = self.sym
        return s.type if s else ''

    def pin_pos(self, p):
        dx, dy = xform(p['x'], p['y'], self.rot, self.flip)
        return (self.x + dx, self.y + dy)

    def pins(self):
        s = self.sym
        if not s:
            return []
        return [(p['name'], self.pin_pos(p), p) for p in s.pins]

    def pin_out(self, p):
        return xform_dir(self.sym.pin_out(p), self.rot, self.flip)

    def bbox(self):
        s = self.sym
        b = (s.body or s.bbox) if s else None
        if not b:
            return (self.x, self.y, self.x, self.y)
        pts = [xform(b[0], b[1], self.rot, self.flip), xform(b[2], b[3], self.rot, self.flip)]
        return (self.x + min(p[0] for p in pts), self.y + min(p[1] for p in pts),
                self.x + max(p[0] for p in pts), self.y + max(p[1] for p in pts))


class Conn:
    """connectivity result"""
    pass


class Sch:
    def __init__(self, path, env=None, create=False):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        self.env = env
        self.is_sym = self.path.endswith('.sym')
        if os.path.isdir(self.path):
            raise XsError('%s is a directory' % path)
        if os.path.exists(self.path):
            self.objs = parse_text(open(self.path, errors='replace').read())
        elif create:
            ver = env.version if env else DEFAULT_VERSION
            self.objs = [Obj('v', {'t': ver}), Obj('G', {'t': ''}),
                         Obj('K', {'t': 'type=subcircuit\nformat="@name @pinlist @symname"\ntemplate="name=x1"\n' if self.is_sym else ''}),
                         Obj('V', {'t': ''}), Obj('S', {'t': ''}), Obj('F', {'t': ''}), Obj('E', {'t': ''})]
            for o in self.objs:
                o.dirty = True
        else:
            raise XsError('no such file: %s' % path)
        self._conn = None

    # --- accessors
    def insts(self):
        return [Inst(o, self) for o in self.objs if o.kind == 'C']

    def inst(self, name):
        for o in self.objs:
            if o.kind == 'C' and o.props.get('name') == name:
                return Inst(o, self)
        raise XsError('no instance named %r' % name)

    def has_inst(self, name):
        return any(o.kind == 'C' and o.props.get('name') == name for o in self.objs)

    def wires(self):
        return [o for o in self.objs if o.kind == 'N']

    def save(self, path=None):
        txt = '\n'.join(o.text() for o in self.objs) + '\n'
        with open(path or self.path, 'w') as f:
            f.write(txt)

    def invalidate(self):
        self._conn = None

    # --- connectivity
    def conn(self):
        if self._conn:
            return self._conn
        wires = self.wires()
        insts = self.insts()
        W = len(wires)
        parent = list(range(W))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            a, b = find(a), find(b)
            if a != b:
                parent[max(a, b)] = min(a, b)

        def newnode():
            parent.append(len(parent))
            return len(parent) - 1

        CS = 160.0
        grid = {}
        for i, w in enumerate(wires):
            f = w.f
            for gx in range(int(math.floor(min(f['x1'], f['x2']) / CS)), int(math.floor(max(f['x1'], f['x2']) / CS)) + 1):
                for gy in range(int(math.floor(min(f['y1'], f['y2']) / CS)), int(math.floor(max(f['y1'], f['y2']) / CS)) + 1):
                    grid.setdefault((gx, gy), []).append(i)

        def wires_at(x, y):
            res = []
            for i in grid.get((int(math.floor(x / CS)), int(math.floor(y / CS))), ()):
                f = wires[i].f
                if on_seg(x, y, f['x1'], f['y1'], f['x2'], f['y2']):
                    res.append(i)
            return res

        def is_end(i, x, y):
            f = wires[i].f
            return (abs(f['x1'] - x) < 1e-3 and abs(f['y1'] - y) < 1e-3) or (abs(f['x2'] - x) < 1e-3 and abs(f['y2'] - y) < 1e-3)

        # wire-wire
        end_touch = {}  # (wire, endidx) -> touches something
        for i, w in enumerate(wires):
            f = w.f
            for ei, (x, y) in enumerate(((f['x1'], f['y1']), (f['x2'], f['y2']))):
                t = False
                for j in wires_at(x, y):
                    if j != i:
                        union(i, j); t = True
                end_touch[(i, ei)] = t
        # pins
        pinnodes = []  # (node, inst, pinname, (x,y), interior_wires)
        ptmap = {}
        missing_syms = []
        labels = []  # (node, inst, type, lab)
        ignored = []
        for inst in insts:
            s = inst.sym
            if s is None:
                missing_syms.append(inst)
                continue
            ign = str(inst.prop('spice_ignore') or '').lower()
            if ign in ('true', '1', 'open'):
                ignored.append(inst)
                for pname, (x, y), p in inst.pins():
                    for j in wires_at(x, y):
                        f = wires[j].f
                        for ei, (ex, ey) in enumerate(((f['x1'], f['y1']), (f['x2'], f['y2']))):
                            if abs(ex - x) < 1e-3 and abs(ey - y) < 1e-3:
                                end_touch[(j, ei)] = 'ign'
                continue
            first = None
            samename = {}
            for pname, (x, y), p in inst.pins():
                nd = newnode()
                if ign == 'short':
                    if first is None:
                        first = nd
                    else:
                        union(nd, first)
                if pname in samename:  # same-named pins of one instance are connected (short.sym)
                    union(nd, samename[pname])
                else:
                    samename[pname] = nd
                key = (round(x * 100), round(y * 100))
                if key in ptmap:
                    union(nd, ptmap[key])
                else:
                    ptmap[key] = nd
                ws = wires_at(x, y)
                interior = []
                for j in ws:
                    union(nd, j)
                    if not is_end(j, x, y):
                        interior.append(j)
                pinnodes.append((nd, inst, pname, (x, y), interior, ws))
                if s.type in LABEL_TYPES:
                    lab = inst.prop('lab')
                    if lab is not None and lab != '':
                        labels.append((nd, inst, s.type, lab))
        # mark wire endpoints touching pins
        for nd, inst, pname, (x, y), interior, ws in pinnodes:
            for j in ws:
                f = wires[j].f
                if abs(f['x1'] - x) < 1e-3 and abs(f['y1'] - y) < 1e-3:
                    end_touch[(j, 0)] = True
                if abs(f['x2'] - x) < 1e-3 and abs(f['y2'] - y) < 1e-3:
                    end_touch[(j, 1)] = True
        byname = {}
        for nd, inst, t, lab in labels:
            if lab in byname:
                union(nd, byname[lab])
            else:
                byname[lab] = nd
        # name components
        comp_names = {}
        for nd, inst, t, lab in labels:
            r = find(nd)
            prio = 0 if t in PORT_TYPES else 1
            comp_names.setdefault(r, []).append((prio, lab))
        netname = {}
        shorts = []
        for r, lst in comp_names.items():
            names = sorted(set(lst))
            netname[r] = names[0][1]
            distinct = sorted(set(n for _, n in lst))
            if len(distinct) > 1:
                shorts.append(distinct)
        cnt = [0]
        order = {}

        def nameof(node):
            r = find(node)
            if r not in netname:
                cnt[0] += 1
                netname[r] = '#n%d' % cnt[0]
            return netname[r]

        C = Conn()
        C.pin_net = {}
        C.nets = {}
        C.pinnodes = pinnodes
        C.labels = labels
        C.missing = missing_syms
        C.ignored = ignored
        C.shorts = shorts
        C.end_touch = end_touch
        C.wires = wires
        C.wire_net = [None] * W
        comp_count = {}
        for nd, inst, pname, xy, interior, ws in pinnodes:
            comp_count[find(nd)] = comp_count.get(find(nd), 0) + 1
        C.isolated = set()
        for nd, inst, pname, xy, interior, ws in pinnodes:
            r = find(nd)
            if comp_count[r] == 1 and not any(find(i) == r for i in ws) and not (inst.type in LABEL_TYPES):
                n = 'NC'
                C.isolated.add((inst.name, pname))
            else:
                n = nameof(nd)
            C.pin_net[(inst.name, pname)] = n
            net = C.nets.setdefault(n, {'pins': [], 'labels': [], 'wires': []})
            if inst.type in LABEL_TYPES:
                net['labels'].append((inst.name, inst.type, inst.prop('lab')))
            else:
                net['pins'].append('%s.%s' % (inst.name, pname))
        for i in range(W):
            n = nameof(i)
            C.wire_net[i] = n
            C.nets.setdefault(n, {'pins': [], 'labels': [], 'wires': []})['wires'].append(i)
        self._conn = C
        return C

    # --- checks
    def warnings(self):
        C = self.conn()
        out = []
        for inst in C.missing:
            out.append('missing symbol %s (inst %s)' % (inst.ref, inst.name))
        for inst in self.insts():
            if inst.ref.endswith('.sch'):
                out.append('instance %s uses a .sch directly (LCC); its pins are not analysed by xs' % inst.name)
        if self.env:
            for ref in sorted(set(i.ref for i in self.insts()) & self.env.fallback):
                out.append('symbol %s is not in the xschem library path (rc %s); found only next to the schematic - '
                           'xschem will NOT find it' % (ref, tilde(self.env.rc) if self.env.rc else 'default'))
        names = {}
        for inst in self.insts():
            names.setdefault(inst.name, 0)
            names[inst.name] += 1
        for n, k in names.items():
            if k > 1:
                out.append('duplicate instance name %s (x%d)' % (n, k))
        for s in C.shorts:
            out.append('SHORT: labels %s are on the same net' % ' '.join(s))
        for (iname, pname) in sorted(C.isolated, key=lambda t: natkey(t[0] + '.' + t[1])):
            out.append('unconnected pin %s.%s' % (iname, pname))
        for nd, inst, pname, xy, interior, ws in C.pinnodes:
            if interior and inst.type not in LABEL_TYPES:
                out.append('pin %s.%s at %s lies on the middle of a wire (implicit connection to %s)' % (
                    inst.name, pname, fpt(xy), C.wire_net[interior[0]]))
        # labels whose pin touches no wire and no other pin (unless another label carries the name)
        for nd, inst, pname, xy, interior, ws in C.pinnodes:
            if inst.type in LABEL_TYPES and not ws:
                others = [p for p in C.pinnodes if p[0] != nd and abs(p[3][0] - xy[0]) < 1e-3 and abs(p[3][1] - xy[1]) < 1e-3]
                if not others:
                    out.append('%s %s (%s) at %s touches no wire/pin (connects nothing)' % (
                        inst.type if inst.type != 'label' else 'label', inst.name, inst.prop('lab'), fpt(xy)))
        for n, net in C.nets.items():
            has_port = any(t in PORT_TYPES for _, t, _ in net['labels'])
            if len(net['pins']) == 1 and not has_port and n not in ('0', 'GND', 'gnd'):
                out.append('net %s has a single connection (%s)' % (n, net['pins'][0]))
            if len(net['pins']) == 0 and net['wires'] and not net['labels']:
                if not any(C.end_touch.get((i, e)) == 'ign' for i in net['wires'] for e in (0, 1)):
                    out.append('floating wire(s) net %s (no pins)' % n)
        for i, w in enumerate(C.wires):
            f = w.f
            if abs(f['x1'] - f['x2']) < EPS and abs(f['y1'] - f['y2']) < EPS:
                out.append('zero-length wire at %s' % fpt((f['x1'], f['y1'])))
                continue
            if abs(f['x1'] - f['x2']) > EPS and abs(f['y1'] - f['y2']) > EPS:
                out.append('diagonal wire %s-%s' % (fpt((f['x1'], f['y1'])), fpt((f['x2'], f['y2']))))
            for ei, xy in enumerate(((f['x1'], f['y1']), (f['x2'], f['y2']))):
                if not C.end_touch.get((i, ei)):
                    out.append('dangling wire end at %s (net %s)' % (fpt(xy), C.wire_net[i]))
        # overlapping collinear wires
        ws = C.wires
        hz = {}
        vt = {}
        for i, w in enumerate(ws):
            f = w.f
            if abs(f['y1'] - f['y2']) < EPS and abs(f['x1'] - f['x2']) > EPS:
                hz.setdefault(round(f['y1'], 3), []).append((min(f['x1'], f['x2']), max(f['x1'], f['x2'])))
            elif abs(f['x1'] - f['x2']) < EPS and abs(f['y1'] - f['y2']) > EPS:
                vt.setdefault(round(f['x1'], 3), []).append((min(f['y1'], f['y2']), max(f['y1'], f['y2'])))
        for tag, dct in (('y', hz), ('x', vt)):
            for c, segs in dct.items():
                segs.sort()
                for a, b in zip(segs, segs[1:]):
                    if b[0] < a[1] - EPS:
                        out.append('overlapping wires on %s=%s (%s..%s)' % (tag, fmt(c), fmt(b[0]), fmt(min(a[1], b[1]))))
        out += self.port_sym_check()
        return out

    def ports(self):
        res = []
        for inst in self.insts():
            if inst.type in PORT_TYPES:
                res.append((inst.prop('lab'), {'ipin': 'in', 'opin': 'out', 'iopin': 'inout'}[inst.type], inst))
        return res

    def port_sym_check(self):
        if self.is_sym:
            return []
        sp = self.path[:-4] + '.sym'
        if not os.path.exists(sp):
            return []
        sym = Sym(parse_text(open(sp).read()), sp, sp)
        sp_pins = {p['name']: p['dir'] for p in sym.pins}
        sc = {}
        for lab, d, _ in self.ports():
            sc[lab] = d
        out = []
        for n in sp_pins:
            if n not in sc:
                out.append('symbol %s has pin %s with no matching port in schematic' % (os.path.basename(sp), n))
            elif sp_pins[n] != sc[n]:
                out.append('pin %s: dir %s in symbol vs %s in schematic' % (n, sp_pins[n], sc[n]))
        for n in sc:
            if n not in sp_pins:
                out.append('schematic port %s missing from symbol %s' % (n, os.path.basename(sp)))
        return out


# ------------------------------------------------------------------- reading
def short_sym(ref):
    return ref[:-4] if ref.endswith('.sym') else ref


def inst_props_str(inst, allp=False, maxlen=60):
    p = inst.o.props
    parts = []
    for k, v in p.items():
        if not allp and k in NOISE_PROPS:
            continue
        v1 = v.replace('\n', '\\n')
        if not allp and len(v1) > maxlen:
            v1 = v1[:maxlen] + '...'
        parts.append('%s=%s' % (k, quote_val(v1) if ' ' in v1 else v1))
    return ' '.join(parts)


def pos_str(inst):
    s = '@%s' % fpt((inst.x, inst.y))
    if inst.rot:
        s += ' R%d' % inst.rot
    if inst.flip:
        s += ' F'
    return s


def cmd_read(sch, opts):
    out = []
    if sch.is_sym:
        return cmd_sym_file(sch, opts)
    C = sch.conn()
    insts = sch.insts()
    rcs = tilde(sch.env.rc) if sch.env and sch.env.rc else 'default'
    out.append('%s  (rc: %s)' % (tilde(sch.path), rcs))
    ports = sch.ports()
    if ports:
        g = {}
        for lab, d, _ in ports:
            g.setdefault(d, []).append(lab)
        out.append('ports: ' + ' | '.join('%s %s' % (d, ' '.join(sorted(g[d], key=natkey))) for d in ('in', 'out', 'inout') if d in g))
    devs, labs, blocks = [], [], []
    for inst in insts:
        t = inst.type
        if t in LABEL_TYPES:
            labs.append(inst)
        elif inst.sym is not None and not inst.sym.pins:
            blocks.append(inst)
        else:
            devs.append(inst)
    filt = opts.get('inst')
    if devs:
        out.append('instances (%d):' % len(devs))
    for inst in devs:
        if filt and not any(fnmatch.fnmatch(inst.name, f) for f in filt):
            continue
        if any(inst.o is i.o for i in C.ignored):
            pins = '(spice_ignore: not connected)'
        else:
            pins = ' '.join('%s=%s' % (pn, C.pin_net.get((inst.name, pn), '?')) for pn, _, _ in inst.pins())
        line = '  %s %s %s %s | %s' % (inst.name, short_sym(inst.ref), pos_str(inst), inst_props_str(inst, opts.get('all')), pins)
        if inst.sym is None:
            line += '  [MISSING SYMBOL]'
        out.append(re.sub(r'  +\|', ' |', line))
        if opts.get('geom'):
            out.append('     pins: ' + ' '.join('%s@%s%s' % (pn, fpt(xy), ARROW[inst.pin_out(p)]) for pn, xy, p in inst.pins()))
    if blocks:
        bl = []
        for b in blocks:
            p = b.o.props
            s = '%s(%s)' % (b.name, os.path.basename(short_sym(b.ref)))
            if p.get('spice_ignore') in ('true', '1'):
                s += '[ignored]'
            v = p.get('value')
            if v and not opts.get('code'):
                v1 = v.strip().replace('\n', '; ')
                s += ' "%s"' % (v1[:50] + ('...' if len(v1) > 50 else ''))
            bl.append(s)
        out.append('blocks: ' + ' | '.join(bl))
    if opts.get('code'):
        for b in blocks:
            v = b.o.props.get('value')
            if v:
                out.append('--- %s value:\n%s' % (b.name, v))
    nl = len(labs)
    out.append('labels: %d   wires: %d   nets: %d' % (nl, len(sch.wires()), len(C.nets)))
    if opts.get('nets') or opts.get('net'):
        out.append('nets:')
        sel = opts.get('net')

        def nkey(n):
            net = C.nets[n]
            return (0 if any(t in PORT_TYPES for _, t, _ in net['labels']) else (2 if n.startswith('#') else 1), natkey(n))
        for n in sorted(C.nets, key=nkey):
            if n == 'NC':
                continue
            if sel and not any(fnmatch.fnmatch(n, s) for s in sel):
                continue
            net = C.nets[n]
            labs_s = ' '.join('%s:%s' % (ln, t if t != 'label' else 'lab') for ln, t, _ in net['labels'])
            out.append('  %s: %s%s' % (n, ' '.join(sorted(net['pins'], key=natkey)), ('  [' + labs_s + ']') if labs_s else ''))
            if opts.get('geom') or sel:
                for i in net['wires']:
                    f = C.wires[i].f
                    out.append('     wire %s %s' % (fpt((f['x1'], f['y1'])), fpt((f['x2'], f['y2']))))
                for ln, t, _ in net['labels']:
                    li = sch.inst(ln)
                    out.append('     %s %s %s' % (t, ln, pos_str(li)))
    w = sch.warnings()
    out.append(fmt_warn(w, opts.get('maxwarn', 30)))
    return '\n'.join(out)


def fmt_warn(w, maxn=30):
    if not w:
        return 'warnings: none'
    s = ['warnings (%d):' % len(w)] + ['  ! ' + x for x in w[:maxn]]
    if len(w) > maxn:
        s.append('  ... %d more' % (len(w) - maxn))
    return '\n'.join(s)


def sym_summary(sym, ref=None, verbose=False):
    out = []
    out.append('%s  type=%s  (%s)' % (ref or sym.path, sym.type or '-', tilde(sym.path)))
    if sym.pins:
        out.append('pins: ' + ' | '.join('%s %s @%s %s' % (p['name'], p['dir'], fpt((p['x'], p['y'])), ARROW[sym.pin_out(p)]) for p in sym.pins))
    else:
        out.append('pins: none')
    if sym.format:
        out.append('format: ' + sym.format.replace('\n', '\\n'))
    if sym.template_str:
        out.append('template: ' + ' '.join('%s=%s' % (k, quote_val(v.replace('\n', '\\n'))) for k, v in sym.template.items()))
    if verbose:
        for k, v in sym.kprops.items():
            if k not in ('type', 'format', 'template'):
                out.append('%s: %s' % (k, v.replace('\n', '\\n')[:200]))
    if sym.bbox:
        out.append('bbox: %s %s' % (fpt(sym.bbox[:2]), fpt(sym.bbox[2:])))
    return '\n'.join(out)


def cmd_sym_file(sch, opts):
    sym = Sym(sch.objs, sch.path, sch.path)
    out = [sym_summary(sym, os.path.basename(sch.path), True)]
    sp = sch.path[:-4] + '.sch'
    if os.path.exists(sp):
        s2 = Sch(sp, sch.env)
        w = s2.port_sym_check()
        out.append('vs %s: %s' % (os.path.basename(sp), 'pins match' if not w else '; '.join(w)))
    if opts.get('geom'):
        for o in sch.objs:
            if o.kind in 'LBPAT':
                out.append('  ' + o.text().replace('\n', '\\n')[:160])
    return '\n'.join(out)


# ------------------------------------------------------------------ edit DSL
def tokenize_script(text):
    """-> list of commands (list of tokens). ; or newline separate commands."""
    cmds, cur, tok, i, n = [], [], None, 0, len(text)
    intok = False
    while i < n:
        c = text[i]
        if c in '"\'':
            q = c
            i += 1
            buf = []
            while i < n and text[i] != q:
                if text[i] == '\\' and q == '"' and i + 1 < n and text[i + 1] in '"\\':
                    buf.append(text[i + 1]); i += 2
                    continue
                buf.append(text[i]); i += 1
            i += 1
            tok = (tok or '') + ''.join(buf)
            intok = True
            continue
        if c == '#' and not intok:
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c in ' \t\r':
            if intok:
                cur.append(tok); tok = None; intok = False
            i += 1
            continue
        if c in '\n;':
            if intok:
                cur.append(tok); tok = None; intok = False
            if cur:
                cmds.append(cur); cur = []
            i += 1
            continue
        if c == '\\' and i + 1 < n and text[i + 1] == '\n':
            i += 2
            continue
        tok = (tok or '') + c
        intok = True
        i += 1
    if intok:
        cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


NUMPT = re.compile(r'^\(?(-?[\d.]+(?:e-?\d+)?),(-?[\d.]+(?:e-?\d+)?)\)?$')
PINREF = re.compile(r'^(?P<inst>[^\s,]+?)\.(?P<pin>[^.+,\s]+?)(?P<off>[+-][-+\d.]+,[-+\d.]+)?$')


class Editor:
    def __init__(self, sch):
        self.sch = sch
        self.env = sch.env
        self.touched = []
        self.log = []
        self.prev = None

    # --- helpers
    def snap(self, v):
        return v

    def pt(self, tok, prev=None):
        """parse point token -> (x, y, pininfo or None)"""
        m = NUMPT.match(tok)
        if m:
            return (float(m.group(1)), float(m.group(2)), None)
        if tok.startswith(('x=', 'y=', 'dx=', 'dy=')):
            if prev is None:
                raise XsError('%s needs a previous point' % tok)
            k, v = tok.split('=', 1)
            if k in ('x', 'y'):
                try:
                    val = float(v)
                except ValueError:
                    q = self.pt(v)
                    val = q[0] if k == 'x' else q[1]
                return (val, prev[1], None) if k == 'x' else (prev[0], val, None)
            val = float(v)
            return (prev[0] + val, prev[1], None) if k == 'dx' else (prev[0], prev[1] + val, None)
        if tok.startswith('d=') or tok.startswith('+') and ',' in tok:
            v = tok[2:] if tok.startswith('d=') else tok[1:]
            dx, dy = v.split(',')
            if prev is None:
                raise XsError('%s needs a previous point' % tok)
            return (prev[0] + float(dx), prev[1] + float(dy), None)
        m = PINREF.match(tok)
        if m:
            inst = self.sch.inst(m.group('inst'))
            s = inst.sym
            if s is None:
                raise XsError('symbol of %s not found' % inst.name)
            p = s.pin(m.group('pin'))
            if p is None:
                raise XsError('%s has no pin %s (pins: %s)' % (inst.name, m.group('pin'), ' '.join(q['name'] for q in s.pins)))
            x, y = inst.pin_pos(p)
            if m.group('off'):
                dx, dy = m.group('off').replace('+-', '-').replace('++', '+').split(',')
                x += float(dx); y += float(dy)
                return (x, y, None)
            return (x, y, (inst, p))
        if tok.count(',') == 1:  # mixed: X,Y where each part is a number or a pin ref
            a, b = tok.split(',')
            try:
                xa = float(a) if re.match(r'^-?[\d.]+$', a) else self.pt(a)[0]
                yb = float(b) if re.match(r'^-?[\d.]+$', b) else self.pt(b)[1]
                return (xa, yb, None)
            except XsError:
                pass
        raise XsError('bad point %r (use x,y | INST.PIN[+dx,dy] | x=.. y=.. dx=.. dy=..)' % tok)

    def kv(self, toks):
        d, flags = {}, []
        for t in toks:
            if '=' in t and not t.startswith('='):
                k, v = t.split('=', 1)
                d[k] = v
            else:
                flags.append(t)
        return d, flags

    def resolve_sym(self, ref):
        ap, hp = self.env.resolve(ref, self.sch.dir)
        if ap:
            return self.env.short_ref(ap, self.sch.dir) if not os.path.isabs(ref) else ref
        cands = self.env.find_sym(ref)
        if len(cands) == 1:
            return cands[0]
        if not cands:
            raise XsError('symbol %r not found (try: xs find %s)' % (ref, os.path.basename(ref)))
        raise XsError('symbol %r ambiguous: %s' % (ref, ' '.join(cands)))

    def autoname(self, prefix):
        used = set(i.name for i in self.sch.insts())
        k = 1
        while '%s%d' % (prefix, k) in used:
            k += 1
        return '%s%d' % (prefix, k)

    def add_obj(self, o):
        o.dirty = True
        # keep wires before components (xschem convention)
        if o.kind == 'N':
            idx = next((i for i, x in enumerate(self.sch.objs) if x.kind == 'C'), len(self.sch.objs))
            self.sch.objs.insert(idx, o)
        else:
            self.sch.objs.append(o)
        self.sch.invalidate()

    def rotflip(self, flags, kw, rot=0, flip=0):
        for f in flags:
            if re.match(r'^R[0-3]$', f):
                rot = int(f[1])
            elif f in ('F', 'flip'):
                flip = 1
        if 'rot' in kw:
            rot = int(kw.pop('rot'))
        if 'flip' in kw:
            flip = int(kw.pop('flip'))
        return rot, flip

    def new_inst(self, name, ref, x, y, rot, flip, props, anchor=None):
        ref = self.resolve_sym(ref)
        s = self.env.sym(ref, self.sch.dir)
        if s is None:
            raise XsError('cannot read symbol %s' % ref)
        tmpl = s.template_str
        if name in (None, '-', 'auto'):
            pre = re.match(r'^([^\d]*)', s.template.get('name', 'x1')).group(1) or 'x'
            name = self.autoname(pre)
        elif self.sch.has_inst(name):
            raise XsError('instance %s already exists' % name)
        if anchor:
            p = s.pin(anchor)
            if p is None:
                raise XsError('symbol %s has no pin %s' % (ref, anchor))
            dx, dy = xform(p['x'], p['y'], rot, flip)
            x, y = x - dx, y - dy
        ps = tmpl.strip('\n') if tmpl else 'name=%s' % name
        ps = set_prop(ps, 'name', name)
        for k, v in props.items():
            ps = set_prop(ps, k, v if v != '' else None)
        o = Obj('C', {'sym': ref, 'x': x, 'y': y, 'rot': rot, 'flip': flip, 'p': ps})
        self.add_obj(o)
        self.touched.append(name)
        return Inst(o, self.sch)

    def add_wire(self, a, b):
        if abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS:
            return
        self.add_obj(Obj('N', {'x1': a[0], 'y1': a[1], 'x2': b[0], 'y2': b[1], 'p': ''}))

    def _obstacles(self):
        pins = [xy for inst in self.sch.insts() for _, xy, _ in inst.pins()]
        ends, segs = [], []
        for w in self.sch.wires():
            f = w.f
            ends += [(f['x1'], f['y1']), (f['x2'], f['y2'])]
            segs.append((f['x1'], f['y1'], f['x2'], f['y2']))
        return pins, ends, segs

    @staticmethod
    def _same(p, q):
        return abs(p[0] - q[0]) < EPS and abs(p[1] - q[1]) < EPS

    def _path_cost(self, path, oa, ob, obs):
        pins, ends, segs = obs
        cost = len(path) - 2  # prefer fewer segments
        for k, (a, b) in enumerate(zip(path, path[1:])):
            if self._same(a, b):
                continue
            for q in pins + ends:  # passing over a pin / wire end creates a connection
                if on_seg(q[0], q[1], a[0], a[1], b[0], b[1]) and not self._same(q, a) and not self._same(q, b):
                    cost += 20
            if 0 < k:  # intermediate corner landing on something
                for q in pins + ends:
                    if self._same(q, a):
                        cost += 20
                for (x1, y1, x2, y2) in segs:
                    if on_seg(a[0], a[1], x1, y1, x2, y2):
                        cost += 20
            hz = abs(a[1] - b[1]) < EPS
            for (x1, y1, x2, y2) in segs:  # perpendicular crossing (legal, but less neat)
                if hz and abs(x1 - x2) < EPS and min(a[0], b[0]) + EPS < x1 < max(a[0], b[0]) - EPS and min(y1, y2) + EPS < a[1] < max(y1, y2) - EPS:
                    cost += 3
                elif not hz and abs(y1 - y2) < EPS and min(a[1], b[1]) + EPS < y1 < max(a[1], b[1]) - EPS and min(x1, x2) + EPS < a[0] < max(x1, x2) - EPS:
                    cost += 3
            for (x1, y1, x2, y2) in segs:  # collinear overlap
                if hz and abs(y1 - y2) < EPS and abs(y1 - a[1]) < EPS:
                    if min(max(a[0], b[0]), max(x1, x2)) - max(min(a[0], b[0]), min(x1, x2)) > EPS:
                        cost += 20
                elif not hz and abs(x1 - x2) < EPS and abs(x1 - a[0]) < EPS:
                    if min(max(a[1], b[1]), max(y1, y2)) - max(min(a[1], b[1]), min(y1, y2)) > EPS:
                        cost += 20
        opp = {'l': 'r', 'r': 'l', 'u': 'd', 'd': 'u'}

        def pen(d, want):
            return 0 if want is None or d == want else (10 if d == opp[want] else 3)
        legs = [(a, b) for a, b in zip(path, path[1:]) if not self._same(a, b)]
        if legs:
            d0 = dir_of_vec(legs[0][1][0] - legs[0][0][0], legs[0][1][1] - legs[0][0][1])
            d1 = dir_of_vec(legs[-1][1][0] - legs[-1][0][0], legs[-1][1][1] - legs[-1][0][1])
            cost += pen(d0, oa) + pen(d1, opp[ob] if ob else None)
        return cost

    def _candidates(self, a, b, mode):
        g = lambda v: round(v / 10.0) * 10
        mx, my = g((a[0] + b[0]) / 2), g((a[1] + b[1]) / 2)
        c = {'hv': [a, (b[0], a[1]), b], 'vh': [a, (a[0], b[1]), b],
             'hvh': [a, (mx, a[1]), (mx, b[1]), b], 'vhv': [a, (a[0], my), (b[0], my), b]}
        if mode in c:
            return [c[mode]]
        return [c['hv'], c['vh'], c['hvh'], c['vhv']]

    def route(self, pts, mode='auto', outs=None):
        outs = outs or [None] * len(pts)
        segs = 0
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            if abs(a[0] - b[0]) < EPS or abs(a[1] - b[1]) < EPS or mode == 'direct':
                path = [a, b]
            else:
                obs = self._obstacles()
                cands = self._candidates(a, b, mode)
                path = min(cands, key=lambda pth: self._path_cost(pth, outs[i], outs[i + 1], obs))
            for p, q in zip(path, path[1:]):
                if not self._same(p, q):
                    self.add_wire(p, q)
                    segs += 1
        return segs

    def outward(self, tok, P):
        if P[2]:
            inst, p = P[2]
            return inst.pin_out(p)
        # wire end? -> continue away from the wire. wire interior -> perpendicular (right / up)
        inter = None
        for w in self.sch.wires():
            f = w.f
            if abs(f['x1'] - P[0]) < EPS and abs(f['y1'] - P[1]) < EPS:
                return dir_of_vec(f['x1'] - f['x2'], f['y1'] - f['y2'])
            if abs(f['x2'] - P[0]) < EPS and abs(f['y2'] - P[1]) < EPS:
                return dir_of_vec(f['x2'] - f['x1'], f['y2'] - f['y1'])
            if inter is None and on_seg(P[0], P[1], f['x1'], f['y1'], f['x2'], f['y2']):
                inter = 'r' if abs(f['x1'] - f['x2']) < EPS else 'u'
        return inter or 'l'

    def place_label(self, symref, tok, net, kw, flags, extra=None):
        P = self.pt(tok)
        d = kw.pop('dir', None) or self.outward(tok, P)
        ln = float(kw.pop('len', 0))
        x, y = P[0], P[1]
        if ln:
            nx, ny = x + DIRV[d][0] * ln, y + DIRV[d][1] * ln
            self.add_wire((x, y), (nx, ny))
            x, y = nx, ny
        ref = self.resolve_sym(symref)
        s = self.env.sym(ref, self.sch.dir)
        bd = s.body_dir()
        rot, flip = None, 0
        if any(re.match(r'^R[0-3]$', f) or f == 'F' for f in flags) or 'rot' in kw:
            rot, flip = self.rotflip(flags, kw)
        else:
            for r, f in ((0, 0), (0, 1), (1, 0), (3, 0), (2, 0), (1, 1), (3, 1), (2, 1)):
                if xform_dir(bd, r, f) == d:
                    rot, flip = r, f
                    break
        props = dict(extra or {})
        props['lab'] = net
        props.update(kw)
        name = props.pop('name', None)
        inst = self.new_inst(name, ref, x, y, rot or 0, flip, props)
        self.log.append('%s %s=%s @%s' % (os.path.basename(short_sym(ref)), inst.name, net, fpt((x, y))))
        return inst

    def pin_positions(self, inst):
        return {pn: xy for pn, xy, _ in inst.pins()}

    def rubber(self, old, new):
        """move wire endpoints sitting at old pin positions to new positions."""
        moved = 0
        for w in list(self.sch.wires()):
            f = w.f
            e = []
            for ei, (x, y) in enumerate(((f['x1'], f['y1']), (f['x2'], f['y2']))):
                for pn, (ox, oy) in old.items():
                    if abs(ox - x) < EPS and abs(oy - y) < EPS:
                        e.append((ei, new[pn]))
                        break
            if not e:
                continue
            horiz = abs(f['y1'] - f['y2']) < EPS
            if len(e) == 2:
                (_, a), (_, b) = e
                f['x1'], f['y1'] = a if e[0][0] == 0 else b
                f['x2'], f['y2'] = b if e[0][0] == 0 else a
                w.dirty = True
                continue
            ei, (nx, ny) = e[0]
            fx, fy = (f['x2'], f['y2']) if ei == 0 else (f['x1'], f['y1'])
            if abs(fx - nx) < EPS or abs(fy - ny) < EPS:
                if ei == 0:
                    f['x1'], f['y1'] = nx, ny
                else:
                    f['x2'], f['y2'] = nx, ny
                w.dirty = True
            else:
                corner = (nx, fy) if horiz else (fx, ny)
                f['x1'], f['y1'], f['x2'], f['y2'] = fx, fy, corner[0], corner[1]
                w.dirty = True
                self.add_wire(corner, (nx, ny))
            moved += 1
        self.sch.invalidate()
        return moved

    # --- commands
    def run(self, cmds):
        for toks in cmds:
            op = toks[0].lower()
            args = toks[1:]
            fn = getattr(self, 'c_' + op, None)
            if fn is None:
                raise XsError('unknown command %r' % op)
            try:
                fn(args)
            except XsError as e:
                raise XsError('%s: %s' % (' '.join(toks)[:120], e))
            except (IndexError, ValueError) as e:
                raise XsError('%s: bad arguments (%s)' % (' '.join(toks)[:120], e))

    def c_place(self, a):
        """place NAME SYM AT [R0-3] [F] [anchor=PIN] [k=v ...]"""
        name, ref, at = a[0], a[1], a[2]
        kw, flags = self.kv(a[3:])
        rot, flip = self.rotflip(flags, kw)
        anchor = kw.pop('anchor', None)
        P = self.pt(at)
        inst = self.new_inst(name, ref, P[0], P[1], rot, flip, kw, anchor)
        self.log.append('placed %s %s %s' % (inst.name, short_sym(inst.ref), pos_str(inst)))

    def c_copy(self, a):
        """copy SRC NEW AT [R..] [F] [anchor=PIN] [k=v]"""
        src = self.sch.inst(a[0])
        new, at = a[1], a[2]
        kw, flags = self.kv(a[3:])
        rot, flip = self.rotflip(flags, kw, src.rot, src.flip)
        anchor = kw.pop('anchor', None)
        P = self.pt(at)
        x, y = P[0], P[1]
        s = src.sym
        if new in ('-', 'auto'):
            new = self.autoname(re.match(r'^([^\d]*)', src.name).group(1) or 'x')
        if self.sch.has_inst(new):
            raise XsError('instance %s exists' % new)
        if anchor:
            p = s.pin(anchor)
            dx, dy = xform(p['x'], p['y'], rot, flip)
            x, y = x - dx, y - dy
        ps = set_prop(src.o.f['p'], 'name', new)
        for k, v in kw.items():
            ps = set_prop(ps, k, v if v != '' else None)
        o = Obj('C', {'sym': src.ref, 'x': x, 'y': y, 'rot': rot, 'flip': flip, 'p': ps})
        self.add_obj(o)
        self.touched.append(new)
        self.log.append('copied %s -> %s %s' % (src.name, new, pos_str(Inst(o, self.sch))))

    def c_move(self, a):
        """move INST AT|by=dx,dy [R..] [F] [anchor=PIN] [norubber]"""
        inst = self.sch.inst(a[0])
        kw, flags = self.kv(a[1:])
        old = self.pin_positions(inst)
        rot, flip = self.rotflip(flags, kw, inst.rot, inst.flip)
        if 'by' in kw:
            dx, dy = [float(v) for v in kw.pop('by').split(',')]
            x, y = inst.x + dx, inst.y + dy
        else:
            pos = [f for f in flags if not re.match(r'^R[0-3]$', f) and f not in ('F', 'flip', 'norubber')]
            if not pos:
                x, y = inst.x, inst.y
            else:
                P = self.pt(pos[0])
                x, y = P[0], P[1]
        anchor = kw.pop('anchor', None)
        if anchor:
            p = inst.sym.pin(anchor)
            dx, dy = xform(p['x'], p['y'], rot, flip)
            x, y = x - dx, y - dy
        # labels sitting directly on the old pin positions follow the pin
        riders = []
        if 'norubber' not in flags:
            for li in self.sch.insts():
                if li.o is inst.o or li.type not in LABEL_TYPES:
                    continue
                for pn, xy, _ in li.pins():
                    hit = [k for k, v in old.items() if self._same(v, xy)]
                    if hit:
                        riders.append((li, hit[0]))
                        break
        f = inst.o.f
        f['x'], f['y'], f['rot'], f['flip'] = x, y, rot, flip
        inst.o.dirty = True
        self.sch.invalidate()
        new = self.pin_positions(inst)
        for li, pn in riders:
            li.o.f['x'] += new[pn][0] - old[pn][0]
            li.o.f['y'] += new[pn][1] - old[pn][1]
            li.o.dirty = True
        n = 0 if 'norubber' in flags else self.rubber(old, new)
        self.touched.append(inst.name)
        self.log.append('moved %s %s (%d wire ends, %d labels followed)' % (inst.name, pos_str(inst), n, len(riders)))

    def c_set(self, a):
        """set INST[,INST2..] k=v ... (k= deletes)"""
        names = a[0].split(',')
        kw, _ = self.kv(a[1:])
        for nm in names:
            insts = [i for i in self.sch.insts() if fnmatch.fnmatch(i.name, nm)] if any(c in nm for c in '*?[') else [self.sch.inst(nm)]
            for inst in insts:
                for k, v in kw.items():
                    if k == 'name' and self.sch.has_inst(v):
                        raise XsError('instance %s exists' % v)
                    inst.o.setp(k, v if v != '' else None)
                self.touched.append(inst.name)
        self.sch.invalidate()
        self.log.append('set %s %s' % (a[0], ' '.join('%s=%s' % kv for kv in kw.items())))

    def c_rename(self, a):
        inst = self.sch.inst(a[0])
        if self.sch.has_inst(a[1]):
            raise XsError('instance %s exists' % a[1])
        inst.o.setp('name', a[1])
        self.touched.append(a[1])
        self.log.append('renamed %s -> %s' % (a[0], a[1]))

    def c_sym(self, a):
        """sym INST NEWSYM  (swap symbol, keep props/position)"""
        inst = self.sch.inst(a[0])
        ref = self.resolve_sym(a[1])
        inst.o.f['sym'] = ref
        inst.o.dirty = True
        self.sch.invalidate()
        self.touched.append(inst.name)
        self.log.append('%s symbol -> %s' % (inst.name, ref))

    def c_rm(self, a):
        """rm NAME...  (instances; glob ok)"""
        rm = set()
        for nm in a:
            hit = [i.name for i in self.sch.insts() if fnmatch.fnmatch(i.name, nm)]
            if not hit:
                raise XsError('no instance %s' % nm)
            rm.update(hit)
        self.sch.objs = [o for o in self.sch.objs if not (o.kind == 'C' and o.props.get('name') in rm)]
        self.sch.invalidate()
        self.log.append('removed %s' % ' '.join(sorted(rm, key=natkey)))

    def c_rmwire(self, a):
        """rmwire P  (wires through point) | rmwire P1 P2 (wires lying on segment P1-P2)"""
        P1 = self.pt(a[0])
        keep, n = [], 0
        P2 = self.pt(a[1]) if len(a) > 1 else None
        for o in self.sch.objs:
            if o.kind == 'N':
                f = o.f
                if P2 is None:
                    hit = on_seg(P1[0], P1[1], f['x1'], f['y1'], f['x2'], f['y2'])
                else:
                    hit = on_seg(f['x1'], f['y1'], P1[0], P1[1], P2[0], P2[1]) and on_seg(f['x2'], f['y2'], P1[0], P1[1], P2[0], P2[1])
                if hit:
                    n += 1
                    continue
            keep.append(o)
        self.sch.objs = keep
        self.sch.invalidate()
        self.log.append('removed %d wire(s)' % n)

    def c_rmnet(self, a):
        """rmnet NET [keeplabels]  (delete all wires of a net, and its labels unless keeplabels)"""
        C = self.sch.conn()
        net = a[0]
        if net not in C.nets:
            raise XsError('no net %s' % net)
        wids = set(id(C.wires[i]) for i in C.nets[net]['wires'])
        labs = set() if 'keeplabels' in a[1:] else set(n for n, t, _ in C.nets[net]['labels'] if t == 'label')
        self.sch.objs = [o for o in self.sch.objs if id(o) not in wids and not (o.kind == 'C' and o.props.get('name') in labs)]
        self.sch.invalidate()
        self.log.append('removed net %s: %d wires, %d labels' % (net, len(wids), len(labs)))

    def c_wire(self, a):
        """wire P1 P2 [P3...] [auto|hv|vh|hvh|vhv|direct]"""
        mode = 'auto'
        pts, outs = [], []
        for t in a:
            if t in ('auto', 'hv', 'vh', 'hvh', 'vhv', 'direct'):
                mode = t
                continue
            P = self.pt(t, pts[-1] if pts else None)
            pts.append((P[0], P[1]))
            outs.append(P[2][0].pin_out(P[2][1]) if P[2] else None)
        if len(pts) < 2:
            raise XsError('wire needs >= 2 points')
        n = self.route(pts, mode, outs)
        self.log.append('wire %s (%d seg)' % (' '.join(fpt(p) for p in pts), n))

    def c_label(self, a):
        """label REF NET [len=L] [dir=l|r|u|d] [sym=lab_pin.sym]"""
        kw, flags = self.kv(a[2:])
        sym = kw.pop('sym', 'lab_pin.sym')
        self.place_label(sym, a[0], a[1], kw, flags)

    def c_net(self, a):
        """net NET REF REF ... [len=L]  (label every ref with NET)"""
        kw, flags = self.kv([t for t in a[1:] if '=' in t])
        refs = [t for t in a[1:] if '=' not in t]
        for r in refs:
            self.place_label(kw.get('sym', 'lab_pin.sym'), r, a[0], {k: v for k, v in kw.items() if k != 'sym'}, [])

    def c_port(self, a):
        """port REF NET in|out|io [len=L] [dir=]"""
        kind = a[2] if len(a) > 2 and '=' not in a[2] else 'io'
        sym = {'in': 'ipin.sym', 'out': 'opin.sym', 'io': 'iopin.sym', 'inout': 'iopin.sym'}[kind]
        kw, flags = self.kv(a[3:] if len(a) > 2 and '=' not in a[2] else a[2:])
        self.place_label(sym, a[0], a[1], kw, flags)

    def c_gnd(self, a):
        kw, flags = self.kv(a[1:])
        net = next((f for f in flags), 'GND')
        self.place_label('gnd.sym', a[0], net, kw, [])

    def c_vdd(self, a):
        kw, flags = self.kv(a[1:])
        net = next((f for f in flags), 'VDD')
        self.place_label('vdd.sym', a[0], net, kw, [])

    def c_text(self, a):
        """text "string" AT [size=0.4] [layer=..]"""
        P = self.pt(a[1])
        kw, flags = self.kv(a[2:])
        sz = float(kw.pop('size', 0.4))
        rot, flip = self.rotflip(flags, kw)
        p = ' '.join('%s=%s' % (k, quote_val(v)) for k, v in kw.items())
        self.add_obj(Obj('T', {'t': a[0], 'x': P[0], 'y': P[1], 'rot': rot, 'flip': flip, 'xs': sz, 'ys': sz, 'p': p}))
        self.log.append('text at %s' % fpt(P))

    # --- graphics / symbol editing
    def c_line(self, a):
        """line P1 P2 [P3 ...] [layer=4]"""
        kw, flags = self.kv(a)
        layer = int(kw.pop('layer', 4))
        pts = []
        for t in flags:
            P = self.pt(t, pts[-1] if pts else None)
            pts.append(P[:2])
        for p, q in zip(pts, pts[1:]):
            self.add_obj(Obj('L', {'layer': layer, 'x1': p[0], 'y1': p[1], 'x2': q[0], 'y2': q[1], 'p': ''}))
        self.log.append('line x%d' % (len(pts) - 1))

    def c_box(self, a):
        """box P1 P2 [layer=4]"""
        kw, flags = self.kv(a)
        layer = int(kw.pop('layer', 4))
        p, q = self.pt(flags[0]), self.pt(flags[1])
        self.add_obj(Obj('B', {'layer': layer, 'x1': min(p[0], q[0]), 'y1': min(p[1], q[1]), 'x2': max(p[0], q[0]), 'y2': max(p[1], q[1]),
                               'p': ' '.join('%s=%s' % (k, quote_val(v)) for k, v in kw.items())}))
        self.log.append('box')

    def c_poly(self, a):
        kw, flags = self.kv(a)
        layer = int(kw.pop('layer', 4))
        pts = []
        for t in flags:
            P = self.pt(t, pts[-1] if pts else None)
            pts.append(P[:2])
        self.add_obj(Obj('P', {'layer': layer, 'pts': pts, 'p': ' '.join('%s=%s' % (k, quote_val(v)) for k, v in kw.items())}))
        self.log.append('poly')

    def c_setk(self, a):
        """setk k=v ...  (symbol global attributes in K block: type, format, template, ...)"""
        kw, _ = self.kv(a)
        K = next((o for o in self.sch.objs if o.kind == 'K'), None)
        if K is None:
            K = Obj('K', {'t': ''})
            self.sch.objs.insert(1, K)
        t = K.f['t']
        for k, v in kw.items():
            t = set_prop(t, k, v if v != '' else None)
        K.f['t'] = t.strip('\n') + '\n'
        K.dirty = True
        self.log.append('K: ' + ' '.join(kw))

    def _pinobj(self, name):
        for o in self.sch.objs:
            if o.kind == 'B' and o.f['layer'] == 5 and o.props.get('name') == name:
                return o
        raise XsError('no pin %s in symbol' % name)

    def c_pin(self, a):
        """pin NAME AT [dir=in|out|inout] [side=l|r|u|d] [len=20] [label=1]  (symbol pin: box + stub + name)"""
        name = a[0]
        P = self.pt(a[1])
        kw, flags = self.kv(a[2:])
        d = kw.pop('dir', 'inout')
        side = kw.pop('side', None)
        ln = float(kw.pop('len', 20))
        x, y = P[0], P[1]
        if side is None:
            side = 'l' if x < 0 else 'r'
        self.add_obj(Obj('B', {'layer': 5, 'x1': x - 2.5, 'y1': y - 2.5, 'x2': x + 2.5, 'y2': y + 2.5,
                               'p': 'name=%s dir=%s%s' % (name, d, ''.join(' %s=%s' % (k, quote_val(v)) for k, v in kw.items() if k != 'label'))}))
        ix, iy = -DIRV[side][0] * ln, -DIRV[side][1] * ln
        if ln:
            self.add_obj(Obj('L', {'layer': 4, 'x1': x, 'y1': y, 'x2': x + ix, 'y2': y + iy, 'p': ''}))
        if kw.get('label', '1') != '0':
            ex, ey = x + ix, y + iy
            if side == 'l':
                T = {'x': ex + 5, 'y': ey - 6, 'rot': 0, 'flip': 0}
            elif side == 'r':
                T = {'x': ex - 5, 'y': ey - 6, 'rot': 0, 'flip': 1}
            elif side == 'u':
                T = {'x': ex - 6, 'y': ey + 5, 'rot': 1, 'flip': 0}
            else:
                T = {'x': ex - 6, 'y': ey - 5, 'rot': 3, 'flip': 0}
            T.update({'t': name, 'xs': 0.2, 'ys': 0.2, 'p': ''})
            self.add_obj(Obj('T', T))
        self.log.append('pin %s %s @%s side=%s' % (name, d, fpt(P), side))

    def c_rmpin(self, a):
        """rmpin NAME...  (removes pin box, its name text and its stub line)"""
        for name in a:
            o = self._pinobj(name)
            x, y = (o.f['x1'] + o.f['x2']) / 2, (o.f['y1'] + o.f['y2']) / 2
            stub = [q for q in self.sch.objs if q.kind == 'L' and ((abs(q.f['x1'] - x) < EPS and abs(q.f['y1'] - y) < EPS) or (abs(q.f['x2'] - x) < EPS and abs(q.f['y2'] - y) < EPS))]
            txt = [q for q in self.sch.objs if q.kind == 'T' and q.f['t'] == name]
            # remove nearest name text only
            if txt:
                txt = [min(txt, key=lambda q: abs(q.f['x'] - x) + abs(q.f['y'] - y))]
            drop = set(id(q) for q in [o] + stub[:1] + txt)
            self.sch.objs = [q for q in self.sch.objs if id(q) not in drop]
            self.log.append('removed pin %s' % name)

    def c_renpin(self, a):
        """renpin OLD NEW"""
        o = self._pinobj(a[0])
        o.setp('name', a[1])
        for q in self.sch.objs:
            if q.kind == 'T' and q.f['t'] == a[0]:
                q.f['t'] = a[1]; q.dirty = True
        self.log.append('renamed pin %s -> %s' % (a[0], a[1]))

    def c_setpin(self, a):
        """setpin NAME k=v ...  (e.g. dir=out)"""
        o = self._pinobj(a[0])
        kw, _ = self.kv(a[1:])
        for k, v in kw.items():
            o.setp(k, v if v != '' else None)
        self.log.append('pin %s %s' % (a[0], ' '.join(kw)))

    def c_movepin(self, a):
        """movepin NAME AT  (moves box, stub line and name text together)"""
        o = self._pinobj(a[0])
        x, y = (o.f['x1'] + o.f['x2']) / 2, (o.f['y1'] + o.f['y2']) / 2
        P = self.pt(a[1])
        dx, dy = P[0] - x, P[1] - y
        stub = [q for q in self.sch.objs if q.kind == 'L' and ((abs(q.f['x1'] - x) < EPS and abs(q.f['y1'] - y) < EPS) or (abs(q.f['x2'] - x) < EPS and abs(q.f['y2'] - y) < EPS))][:1]
        txt = [q for q in self.sch.objs if q.kind == 'T' and q.f['t'] == a[0]]
        txt = [min(txt, key=lambda q: abs(q.f['x'] - x) + abs(q.f['y'] - y))] if txt else []
        for q in [o] + stub:
            q.f['x1'] += dx; q.f['x2'] += dx; q.f['y1'] += dy; q.f['y2'] += dy; q.dirty = True
        for q in txt:
            q.f['x'] += dx; q.f['y'] += dy; q.dirty = True
        self.log.append('moved pin %s to %s' % (a[0], fpt(P)))


def update_wire_labels(sch):
    if sch.is_sym:
        return
    C = sch.conn()
    for i, w in enumerate(C.wires):
        n = C.wire_net[i]
        cur = w.props.get('lab')
        if n.startswith('#'):
            if cur is None or not cur.startswith('#'):
                w.setp('lab', '#net' + n[2:])
        elif cur != n:
            w.setp('lab', n)


def net_snapshot(sch):
    """(inst,pin) -> (net, frozenset of device pins on that net) for non-label pins"""
    C = sch.conn()
    res = {}
    for n, net in C.nets.items():
        mem = frozenset(net['pins'])
        for pr in net['pins']:
            i, _, p = pr.rpartition('.')
            res[(i, p)] = (n, mem if n.startswith('#') or n == 'NC' else None)
    return res


def cmd_edit(path, script, opts):
    env = Env(os.path.dirname(os.path.abspath(path)))
    create = not os.path.exists(path)
    sch = Sch(path, env, create=True)
    before = set(sch.warnings()) if not create and not sch.is_sym else set()
    snap0 = net_snapshot(sch) if not create and not sch.is_sym else {}
    ed = Editor(sch)
    ed.run(tokenize_script(script))
    out = ['%s %s' % ('created' if create else 'edited', tilde(sch.path))]
    out += ['  ' + l for l in ed.log]
    if sch.is_sym:
        if not opts.get('dry'):
            sch.save()
        sym = Sym(sch.objs, sch.path, sch.path)
        out.append(sym_summary(sym, os.path.basename(path)))
        return '\n'.join(out)
    update_wire_labels(sch)
    if not opts.get('dry'):
        sch.save()
    else:
        out.append('(dry run, not saved)')
    C = sch.conn()
    seen = []
    for nm in ed.touched:
        if nm in seen or not sch.has_inst(nm):
            continue
        seen.append(nm)
        inst = sch.inst(nm)
        if inst.type in LABEL_TYPES or (inst.sym is not None and not inst.sym.pins):
            continue
        out.append('  %s: %s' % (nm, ' '.join('%s=%s' % (pn, C.pin_net.get((nm, pn), '?')) for pn, _, _ in inst.pins())))
    if snap0:
        snap1 = net_snapshot(sch)
        ch = []
        for k, (n1, mem1) in sorted(snap1.items(), key=lambda t: natkey('.'.join(t[0]))):
            if k[0] in seen or k not in snap0:
                continue
            n0, mem0 = snap0[k]
            if n0 == n1 and (not n0.startswith('#') or mem0 == mem1):
                continue
            if n0.startswith('#') and n1.startswith('#') and mem0 == mem1:
                continue
            ch.append('%s.%s: %s -> %s' % (k[0], k[1], n0, n1))
        gone = [k for k in snap0 if k not in snap1 and k[0] not in seen]
        if ch:
            out.append('net changes (%d): %s%s' % (len(ch), '; '.join(ch[:40]), ' ...' if len(ch) > 40 else ''))
    w = sch.warnings()
    new = [x for x in w if x not in before]
    old = len(w) - len(new)
    if new:
        out.append(fmt_warn(new).replace('warnings', 'new warnings'))
    else:
        out.append('new warnings: none')
    if old:
        out.append('(%d pre-existing warnings; see xs check)' % old)
    return '\n'.join(out)


# ----------------------------------------------------------------- mksym
POWER_RE = re.compile(r'^(a|d)?v(dd|cc|pwr|dda|ddd|ddio)\w*$|^vdd', re.I)
GROUND_RE = re.compile(r'^(a|d)?(vss|gnd|vgnd)\w*$|^vss|^gnd', re.I)


def cmd_mksym(path, opts):
    env = Env(os.path.dirname(os.path.abspath(path)))
    sch = Sch(path, env)
    ports = sch.ports()
    if not ports:
        raise XsError('schematic has no ipin/opin/iopin ports')
    out_path = opts.get('o') or path[:-4] + '.sym'
    if os.path.exists(out_path) and not opts.get('force'):
        raise XsError('%s exists (use --force to overwrite)' % out_path)
    seen = set()
    groups = {'l': [], 'r': [], 'u': [], 'd': []}
    order = sorted(ports, key=lambda p: (p[2].y, p[2].x))
    over = {}
    for side in 'lrud':
        for n in (opts.get(side) or '').split(','):
            if n:
                over[n] = side
    for lab, d, inst in order:
        if lab in seen:
            continue
        seen.add(lab)
        if lab in over:
            side = over[lab]
        elif d == 'in':
            side = 'l'
        elif d == 'out':
            side = 'r'
        elif POWER_RE.match(lab):
            side = 'u'
        elif GROUND_RE.match(lab):
            side = 'd'
        else:
            side = 'r'
        groups[side].append((lab, d))
    G = 20
    cw = 7.0  # approx char width at 0.2 text
    lw = max([len(n) for n, _ in groups['l']] + [0]) * cw
    rw = max([len(n) for n, _ in groups['r']] + [0]) * cw
    nh = max(len(groups['u']), len(groups['d']))
    width = max(lw + rw + 40, (nh + 1) * G * 2, 80)
    width = int(math.ceil(width / (2 * G)) * 2 * G)
    nv = max(len(groups['l']), len(groups['r']), 1)
    height = int((nv + 1) * G)
    if height % 20:
        height += 20 - height % 20
    hw, hh = width // 2, height // 2
    hh = int(math.ceil(hh / G) * G)
    L = []
    L.append('L 4 %d %d %d %d {}' % (-hw, -hh, hw, -hh))
    L.append('L 4 %d %d %d %d {}' % (hw, -hh, hw, hh))
    L.append('L 4 %d %d %d %d {}' % (hw, hh, -hw, hh))
    L.append('L 4 %d %d %d %d {}' % (-hw, hh, -hw, -hh))
    pins = []

    def vpos(k, n):
        start = -((n - 1) * G) // 2
        start = int(round(start / G) * G) if n % 2 else start
        return start + k * G

    for side in 'lrud':
        lst = groups[side]
        n = len(lst)
        for k, (nm, d) in enumerate(lst):
            if side in 'lr':
                y = -(n - 1) * G // 2 + k * G
                y = int(round(y / 10.0) * 10)
                x0 = -hw if side == 'l' else hw
                x = x0 - G if side == 'l' else x0 + G
                L.append('L 4 %d %d %d %d {}' % (x, y, x0, y))
                if side == 'l':
                    L.append('T {%s} %d %d 0 0 0.2 0.2 {}' % (nm, x0 + 5, y - 6))
                else:
                    L.append('T {%s} %d %d 0 1 0.2 0.2 {}' % (nm, x0 - 5, y - 6))
                pins.append((nm, d, x, y))
            else:
                x = -(n - 1) * G // 2 + k * G
                x = int(round(x / 10.0) * 10)
                y0 = -hh if side == 'u' else hh
                y = y0 - G if side == 'u' else y0 + G
                L.append('L 4 %d %d %d %d {}' % (x, y, x, y0))
                if side == 'u':
                    L.append('T {%s} %d %d 3 0 0.2 0.2 {}' % (nm, x - 6, y0 + 5 + len(nm) * cw))
                else:
                    L.append('T {%s} %d %d 1 0 0.2 0.2 {}' % (nm, x + 6, y0 - 5 - len(nm) * cw))
                pins.append((nm, d, x, y))
    B = ['B 5 %s %s %s %s {name=%s dir=%s}' % (fmt(x - 2.5), fmt(y - 2.5), fmt(x + 2.5), fmt(y + 2.5), nm, d) for nm, d, x, y in pins]
    kblock = 'type=subcircuit\nformat="@name @pinlist @symname"\ntemplate="name=x1"\n'
    if os.path.exists(out_path) and opts.get('force'):
        old = parse_text(open(out_path).read())
        K = next((o for o in old if o.kind == 'K'), None)
        if K and K.f['t'].strip():
            kblock = K.f['t']
    txt = ['v {%s}' % env.version, 'G {}', 'K {%s}' % file_escape(kblock), 'V {}', 'S {}', 'F {}', 'E {}'] + L + B + [
        'T {@symname} %d %d 0 0 0.3 0.3 {}' % (-hw, -hh - 40 if not groups['u'] else -hh - 40 - 60),
        'T {@name} %d %d 0 0 0.2 0.2 {}' % (-hw, hh + 5 if not groups['d'] else hh + 65)]
    open(out_path, 'w').write('\n'.join(txt) + '\n')
    sym = Sym(parse_text('\n'.join(txt)), out_path, out_path)
    return 'wrote %s\n%s' % (tilde(out_path), sym_summary(sym, os.path.basename(out_path)))


# ------------------------------------------------------- container commands
def cmd_netlist(path, opts):
    env = Env(os.path.dirname(os.path.abspath(path)))
    od = os.path.join(CACHE, 'netlist')
    os.makedirs(od, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    for ext in ('.spice', '.spc', '.cir'):
        try:
            os.remove(os.path.join(od, base + ext))
        except OSError:
            pass
    flags = '-n -s -o %s' % shlex.quote(od)
    if opts.get('flat'):
        flags += ' --tcl "set flat_netlist 1"'
    if opts.get('lvs'):
        flags += ' --tcl "set lvs_netlist 1"'
    rc, out, err = run_container(xschem_cmd(env.rc, '%s %s' % (flags, shlex.quote(os.path.abspath(path)))), env.cwd)
    nf = os.path.join(od, base + '.spice')
    if not os.path.exists(nf):
        raise XsError('netlist failed:\n' + (out + '\n' + err)[-1500:])
    txt = open(nf).read()
    if opts.get('o'):
        open(opts['o'], 'w').write(txt)
    msgs = [l for l in (out + '\n' + err).splitlines() if re.search(r'warning|error|short|not found|unconnected', l, re.I)]
    res = txt if not opts.get('o') else 'wrote %s (%d lines)' % (opts['o'], txt.count('\n'))
    if msgs:
        res += '\nxschem messages:\n' + '\n'.join('  ' + m for m in msgs[:30])
    return res


def cmd_render(path, opts):
    env = Env(os.path.dirname(os.path.abspath(path)))
    sch = Sch(path, env)
    base = os.path.splitext(os.path.basename(path))[0]
    out_png = os.path.abspath(opts.get('o') or os.path.join(CACHE, 'render', base + '.png'))
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    svg = out_png[:-4] + '.svg' if out_png.endswith('.png') else out_png + '.svg'
    # aspect from our own bbox
    boxes = []
    if not sch.is_sym:
        for i in sch.insts():
            boxes.append(i.bbox())
        for w in sch.wires():
            f = w.f
            boxes.append((min(f['x1'], f['x2']), min(f['y1'], f['y2']), max(f['x1'], f['x2']), max(f['y1'], f['y2'])))
    else:
        s = Sym(sch.objs, path, path)
        if s.bbox:
            boxes.append(s.bbox)
    if boxes:
        bw = max(b[2] for b in boxes) - min(b[0] for b in boxes) + 120
        bh = max(b[3] for b in boxes) - min(b[1] for b in boxes) + 120
    else:
        bw, bh = 1000, 700
    W = int(opts.get('width') or 1400)
    H = int(max(300, min(2400, W * bh / max(bw, 1))))
    dark = '1' if opts.get('dark') else '0'
    tcl = ('set dark_colorscheme %s; catch {xschem build_colors}; xschem zoom_full; '
           'xschem print svg %s %d %d; exit' % (dark, shlex.quote(svg), W, H))
    py = ("import cairosvg,sys; cairosvg.svg2png(url=sys.argv[1], write_to=sys.argv[2], background_color=sys.argv[3])")
    script = '%s && python3 -c %s %s %s %s' % (
        xschem_cmd(env.rc, '--command %s %s' % (shlex.quote(tcl), shlex.quote(os.path.abspath(path)))),
        shlex.quote(py), shlex.quote(svg), shlex.quote(out_png), 'black' if dark == '1' else 'white')
    rc, out, err = run_container(script, env.cwd)
    if not os.path.exists(out_png):
        raise XsError('render failed:\n' + (out + '\n' + err)[-1500:])
    if not opts.get('svg'):
        try:
            os.remove(svg)
        except OSError:
            pass
    return 'rendered %s (%dx%d)' % (out_png, W, H)


def cmd_xcheck(path, opts):
    """cross-check our connectivity against xschem's own."""
    env = Env(os.path.dirname(os.path.abspath(path)))
    sch = Sch(path, env)
    tcl = ('xschem rebuild_connectivity; set n [xschem get instances]; '
           'for {set i 0} {$i < $n} {incr i} { set nm [xschem getprop instance $i name]; '
           'foreach p [xschem instance_pins $nm] { puts "XC\\t$nm\\t$p\\t[xschem instance_net $nm $p]" } }; exit')
    rc, out, err = run_container(xschem_cmd(env.rc, '--command %s %s' % (shlex.quote(tcl), shlex.quote(os.path.abspath(path)))), env.cwd)
    theirs = {}
    for l in out.splitlines():
        if l.startswith('XC\t'):
            parts = l.split('\t')
            if len(parts) >= 4:
                theirs[(parts[1], parts[2])] = parts[3]
    if not theirs:
        raise XsError('xschem returned no connectivity:\n' + (out + err)[-1000:])
    return compare_conn(sch, theirs)


def expand_bus(name):
    out = []
    for part in name.split(','):
        m = re.match(r'^(.*)\[(\d+):(\d+)\]$', part)
        if m:
            a, b = int(m.group(2)), int(m.group(3))
            step = -1 if a > b else 1
            out += ['%s[%d]' % (m.group(1), i) for i in range(a, b + step, step)]
        else:
            out.append(part)
    return ','.join(out)


def compare_conn(sch, theirs):
    C = sch.conn()
    mine = C.pin_net
    skip = set(i.name for i in C.ignored)
    if sch.env:
        skip |= set(i.name for i in sch.insts() if i.ref in sch.env.fallback)
    m2t, t2m, bad = {}, {}, []
    for k, mn in mine.items():
        if k[0] in skip:
            continue
        if k not in theirs:
            bad.append('pin %s.%s unknown to xschem' % k)
            continue
        tn = theirs[k]
        if mn == 'NC':
            continue
        mn = expand_bus(mn) if not mn.startswith('#') else mn
        if mn in m2t and m2t[mn] != tn:
            bad.append('%s.%s: mine %s, xschem %s (but %s also maps to %s)' % (k[0], k[1], mn, tn, mn, m2t[mn]))
        m2t.setdefault(mn, tn)
        if tn in t2m and t2m[tn] != mn:
            bad.append('%s.%s: xschem net %s maps to both %s and %s' % (k[0], k[1], tn, t2m[tn], mn))
        t2m.setdefault(tn, mn)
        if not mn.startswith('#') and mn != tn:
            bad.append('%s.%s: name mine=%s xschem=%s' % (k[0], k[1], mn, tn))
    for k in theirs:
        if k not in mine and k[0] not in skip:
            bad.append('pin %s.%s only known to xschem' % k)
    if bad:
        return 'XCHECK MISMATCH (%d):\n' % len(bad) + '\n'.join('  ' + b for b in bad[:40])
    return 'xcheck OK: %d pins, %d nets agree with xschem' % (len(mine), len(m2t))


# ------------------------------------------------------------------- find
def cmd_find(env, pats, opts):
    allsyms = env.all_syms()
    res = []
    for ap, hp in allsyms.items():
        b = os.path.basename(ap)[:-4]
        for p in pats:
            if fnmatch.fnmatch(b.lower(), p.lower()) or (not any(c in p for c in '*?[') and p.lower() in b.lower()):
                res.append(ap)
                break
    out = []
    lim = int(opts.get('max', 40))
    refs = {}
    for ap in res:
        r = env.short_ref(ap)
        refs.setdefault(r, ap)
    for r in sorted(refs, key=natkey)[:lim]:
        s = env.sym(r)
        if s is None:
            continue
        out.append('%s  [%s] %s' % (short_sym(r), s.type or '-', ' '.join(p['name'] for p in s.pins)))
    if len(refs) > lim:
        out.append('... %d more (use --max N)' % (len(refs) - lim))
    return '\n'.join(out) if out else 'no symbols match'


# -------------------------------------------------------------------- main
HELP = r"""xs - compact xschem tool.   Usage: xs CMD FILE [opts]

read FILE [-n|--nets] [--net PAT] [-g|--geom] [-i PAT] [-a|--all] [-c|--code]
     schematic summary: ports, instances (sym, pos, props | pin=net), warnings.
     -n net list, --net PAT nets matching (+geometry), -g pin coords/outward dir,
     -i only instances matching glob, -a all props untruncated, -c code blocks.
     On .sym files: pins/format/template.
sym REF            symbol info (REF = lib ref like sg13g2_pr/sg13_lv_nmos or a path)
find PAT... [--max N]  search symbols in the library path (substring or glob)
check FILE         warnings only (connectivity sanity)
edit FILE [-e 'cmds'] [-f script] [--dry]   apply edit DSL (stdin if no -e/-f);
     creates FILE if missing. Prints log, pin->net of touched instances, new warnings.
mksym SCH [-o SYM] [--force] [--l a,b --r c --u d --d e]  box symbol from ports
netlist SCH [--flat] [--lvs] [-o FILE]   spice netlist via xschem (container)
render FILE [-o out.png] [--dark] [--width N] [--svg]  PNG via xschem (container)
xcheck SCH         compare xs connectivity with xschem's own (container)
env [--refresh]    show rc/pathlist (refresh caches)

EDIT DSL (one command per line or ';'-separated, # comments, "quotes" for spaces):
 points: x,y | INST.PIN | INST.PIN+dx,dy | (in wire/line chains) x=V y=V dx=V dy=V
         x=/y= also accept a pin ref: y=M2.S
 place NAME|auto SYM AT [R0-3] [F] [anchor=PIN] [k=v..]  (anchor: PIN lands on AT)
 copy SRC NEW AT [R..] [F] [anchor=PIN] [k=v..]
 move INST AT|by=dx,dy [R..] [F] [anchor=PIN] [norubber]   (wires follow pins)
 set INST[,INST|glob] k=v..  (k= deletes)   rename OLD NEW   sym INST NEWSYM
 rm NAME|glob..   rmwire P [P2]   rmnet NET [keeplabels]
 wire P1 P2 [P3..] [auto|hv|vh|hvh|vhv|direct]  (auto: picks L/Z avoiding pins, leaves pins outward)
 label REF NET [len=L] [dir=l|r|u|d] [sym=lab_pin.sym]   (auto-oriented outward)
 net NET REF REF ..       port REF NET in|out|io [len=L]
 gnd REF [NET]   vdd REF [NET]   text "str" AT [size=0.4]
 symbol files: pin NAME AT dir=in|out|inout side=l|r|u|d [len=20]; rmpin; renpin OLD NEW;
               setpin NAME k=v; movepin NAME AT; setk k=v (K block); line; box; poly
"""


def parse_opts(argv, flags_with_val=()):
    opts, pos = {}, []
    alias = {'-n': 'nets', '-g': 'geom', '-a': 'all', '-c': 'code', '-i': 'inst', '-e': 'e', '-f': 'f', '-o': 'o'}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('-') and not NUMPT.match(a):
            k = alias.get(a, a.lstrip('-'))
            if k in flags_with_val:
                v = argv[i + 1]
                i += 1
                if k in ('inst', 'net'):
                    opts.setdefault(k, []).extend(v.split(','))
                else:
                    opts[k] = v
            else:
                opts[k] = True
        else:
            pos.append(a)
        i += 1
    return opts, pos


def main(argv):
    if not argv or argv[0] in ('help', '-h', '--help'):
        print(HELP)
        return 0
    cmd = argv[0]
    VAL = ('inst', 'net', 'e', 'f', 'o', 'width', 'l', 'r', 'u', 'd', 'max', 'rc')
    opts, pos = parse_opts(argv[1:], VAL)
    try:
        if cmd == 'read':
            for p in pos:
                env = Env(os.path.dirname(os.path.abspath(p)))
                print(cmd_read(Sch(p, env), opts))
        elif cmd == 'check':
            for p in pos:
                env = Env(os.path.dirname(os.path.abspath(p)))
                s = Sch(p, env)
                print('%s: %s' % (os.path.basename(p), fmt_warn(s.warnings(), 200)))
        elif cmd == 'sym':
            env = Env(os.getcwd())
            for r in pos:
                if os.path.exists(r) and r.endswith('.sym'):
                    s = Sym(parse_text(open(r).read()), os.path.abspath(r), r)
                    print(sym_summary(s, r, True))
                    continue
                s = env.sym(r)
                if s is None:
                    c = env.find_sym(r)
                    if len(c) == 1:
                        s, r = env.sym(c[0]), c[0]
                    else:
                        print('%s: not found%s' % (r, (' (candidates: %s)' % ' '.join(c)) if c else ''))
                        continue
                print(sym_summary(s, short_sym(r), opts.get('all')))
        elif cmd == 'find':
            env = Env(os.getcwd())
            print(cmd_find(env, pos, opts))
        elif cmd == 'edit':
            if 'e' in opts:
                script = opts['e']
            elif 'f' in opts:
                script = open(opts['f']).read()
            else:
                script = sys.stdin.read()
            print(cmd_edit(pos[0], script, opts))
        elif cmd == 'mksym':
            print(cmd_mksym(pos[0], opts))
        elif cmd == 'netlist':
            print(cmd_netlist(pos[0], opts))
        elif cmd == 'render':
            for p in pos:
                print(cmd_render(p, opts))
        elif cmd == 'xcheck':
            for p in pos:
                print('%s: %s' % (os.path.basename(p), cmd_xcheck(p, opts)))
        elif cmd == 'env':
            env = Env(pos[0] if pos else os.getcwd(), refresh=bool(opts.get('refresh')))
            print('rc: %s\nversion: %s\npathlist:\n  %s' % (env.rc or 'default', env.version, '\n  '.join(env.pathlist)))
        else:
            print('unknown command %s\n%s' % (cmd, HELP))
            return 2
    except XsError as e:
        print('ERROR: %s' % e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
