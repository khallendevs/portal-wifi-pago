#!/usr/bin/env python3
"""
Portal cativo de Wi-Fi pago para Termux (Android com ROOT) - v2.3 com atualizações automáticas.

Cliente conecta -> recebe um acesso inicial único de 5 minutos -> contata o administrador pelo WhatsApp para mais tempo.
Mercado Pago pode ser habilitado opcionalmente pelo painel ou terminal.

ADMIN (web):      http://IP_DO_HOTSPOT:8080/admin     (senha padrão: admin123)
ADMIN (terminal): python portal.py            -> sobe o portal + menu de administração
                  python portal.py servidor   -> só o portal (use em 2º plano / nohup)
                  python portal.py ajuda      -> lista de comandos (funcionam com o portal rodando)
ATUALIZAÇÃO:      automática a cada 15 min (opcional) | painel web > Sistema

Variáveis opcionais: WIFI_IFACE=wlan0  PORTAL_PORT=8080  MP_TOKEN=...  NOME_REDE=...  PRECO=2.00  MINUTOS=5

AO PUBLICAR UMA NOVA VERSÃO: aumente o número em VERSION para facilitar a identificação.
O atualizador compara também o conteúdo do script, então não depende só do número.
"""
VERSION = "2.3.0"
ID_PROGRAMA = "wifi-portal-termux"
URL_ATUALIZACAO_PADRAO = "https://raw.githubusercontent.com/khallendevs/portal-wifi-pago/main/portal.py"

import os, sys, re, ast, json, time, uuid, html, hmac, glob, shutil, hashlib, secrets, shlex, sqlite3
import getpass, threading, subprocess, atexit, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlunparse, parse_qsl, urlencode, quote

IFACE = os.environ.get("WIFI_IFACE", "wlan0")
PORTA = int(os.environ.get("PORTAL_PORT", "8080"))
DB = os.environ.get("PORTAL_DB", os.path.expanduser("~/wifi_portal.db"))
ARQUIVO = os.path.abspath(__file__)
SENHA_PADRAO = "admin123"
e = html.escape

# ====================================================================== banco
db_lock = threading.RLock()
conn = sqlite3.connect(DB, check_same_thread=False, timeout=15)
conn.execute("PRAGMA journal_mode=WAL")

def _colunas(tabela):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}

def iniciar_db():
    with db_lock:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS planos(id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT,
            minutos INTEGER, preco REAL, ativo INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sessoes(mac TEXT PRIMARY KEY, ip TEXT, expira INTEGER);
        CREATE TABLE IF NOT EXISTS acessos_iniciais(mac TEXT PRIMARY KEY, concedido_em INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS pagamentos(id TEXT PRIMARY KEY, mac TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS vouchers(codigo TEXT PRIMARY KEY, minutos INTEGER,
            criado INTEGER, usado_por TEXT, usado_em INTEGER);
        CREATE TABLE IF NOT EXISTS bloqueados(mac TEXT PRIMARY KEY, motivo TEXT, criado INTEGER);
        CREATE TABLE IF NOT EXISTS fixos(mac TEXT PRIMARY KEY, nome TEXT);
        """)
        for col, tipo in [("plano_nome", "TEXT"), ("minutos", "INTEGER"), ("metodo", "TEXT"),
                          ("valor", "REAL"), ("criado", "INTEGER"), ("pago_em", "INTEGER")]:
            if col not in _colunas("pagamentos"):
                conn.execute(f"ALTER TABLE pagamentos ADD COLUMN {col} {tipo}")
        mp = os.environ.get("MP_TOKEN", "")
        padroes = {
            "nome_rede": os.environ.get("NOME_REDE", "Wi-Fi Livre"),
            "metodo_mp": "0", "mp_token": mp,
            "metodo_pix_manual": "0", "pix_chave": "", "pix_nome": "",
            "metodo_voucher": "0",
            "metodo_teste": "0", "whatsapp_admin": "",
            "admin_somente_local": "0",
            "senha_hash": hash_senha(SENHA_PADRAO), "senha_padrao": "1",
            "url_atualizacao": URL_ATUALIZACAO_PADRAO, "upd_auto": "1",
            "upd_versao_remota": "", "upd_ultima": "0", "upd_erro": "",
        }
        for k, v in padroes.items():
            conn.execute("INSERT OR IGNORE INTO config VALUES(?,?)", (k, v))
        url_antiga = ("https://www.dropbox.com/scl/fi/vdycf70tjtowv3u4ud0aa/portal.py"
                      "?rlkey=pkotvd5w92ilgzxjccdl61fyv&st=wsp6an0s&dl=1")
        conn.execute("UPDATE config SET v=? WHERE k='url_atualizacao' AND v=?",
                     (URL_ATUALIZACAO_PADRAO, url_antiga))
        # Migração única: interrompe checkout legado e exige ativação explícita do Mercado Pago.
        # Token, histórico e cadastros são preservados; pagamentos novos ficam pausados.
        if not conn.execute("SELECT 1 FROM config WHERE k='politica_acesso_v1'").fetchone():
            conn.execute("UPDATE config SET v='0' WHERE k IN ('metodo_mp','metodo_pix_manual','metodo_voucher','metodo_teste')")
            conn.execute("INSERT OR REPLACE INTO config VALUES('politica_acesso_v1','1')")
        if not conn.execute("SELECT 1 FROM planos").fetchone():
            conn.execute("INSERT INTO planos(nome,minutos,preco) VALUES(?,?,?)",
                         ("Plano padrão", int(os.environ.get("MINUTOS", "5")),
                          float(os.environ.get("PRECO", "2.00"))))
        conn.commit()

def q(sql, *p):
    with db_lock:
        return conn.execute(sql, p).fetchall()

def x(sql, *p):
    with db_lock:
        cur = conn.execute(sql, p)
        conn.commit()
        return cur

def cfg(k):
    r = q("SELECT v FROM config WHERE k=?", k)
    return r[0][0] if r else ""

def cfg_set(k, v):
    x("REPLACE INTO config VALUES(?,?)", k, str(v))

# ================================================================== segurança
def hash_senha(s, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", s.encode(), salt.encode(), 150_000).hex()

def confere_senha(s):
    atual = cfg("senha_hash")
    return hmac.compare_digest(hash_senha(s, atual.partition("$")[0]), atual)

def definir_senha(nova):
    cfg_set("senha_hash", hash_senha(nova))
    cfg_set("senha_padrao", "1" if nova == SENHA_PADRAO else "0")

def validar_senha_nova(s):
    if len(s) < 6:
        return "A senha precisa ter pelo menos 6 caracteres."
    if s == SENHA_PADRAO:
        return "Escolha uma senha diferente da padrão."
    return ""

FALHAS = {}  # chave -> (contagem, bloqueado_até)

def limitado(chave):
    return FALHAS.get(chave, (0, 0))[1] > time.time()

def falhou(chave, limite=5, espera=300):
    n, _ = FALHAS.get(chave, (0, 0))
    n += 1
    FALHAS[chave] = (0, time.time() + espera) if n >= limite else (n, 0)

# =================================================================== firewall
def sh(cmd):
    try:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            cmd = ["su", "-c", " ".join(shlex.quote(c) for c in cmd)]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as ex:
        return subprocess.CompletedProcess(cmd, 127, "", str(ex))

def ipt(*a):
    return sh(["iptables", *a])

def firewall_setup():
    ipt("-t", "nat", "-N", "WIFI_PORTAL"); ipt("-N", "WIFI_FWD")
    ipt("-t", "nat", "-F", "WIFI_PORTAL"); ipt("-F", "WIFI_FWD")
    ipt("-t", "nat", "-A", "WIFI_PORTAL", "-p", "tcp", "--dport", "80",
        "-j", "REDIRECT", "--to-ports", str(PORTA))
    ipt("-A", "WIFI_FWD", "-p", "udp", "--dport", "53", "-j", "ACCEPT")
    ipt("-A", "WIFI_FWD", "-p", "tcp", "--dport", "53", "-j", "ACCEPT")
    ipt("-A", "WIFI_FWD", "-j", "DROP")
    if ipt("-t", "nat", "-C", "PREROUTING", "-i", IFACE, "-j", "WIFI_PORTAL").returncode != 0:
        ipt("-t", "nat", "-I", "PREROUTING", "1", "-i", IFACE, "-j", "WIFI_PORTAL")
    if ipt("-C", "FORWARD", "-i", IFACE, "-j", "WIFI_FWD").returncode != 0:
        ipt("-I", "FORWARD", "1", "-i", IFACE, "-j", "WIFI_FWD")

def firewall_cleanup():
    ipt("-t", "nat", "-D", "PREROUTING", "-i", IFACE, "-j", "WIFI_PORTAL")
    ipt("-D", "FORWARD", "-i", IFACE, "-j", "WIFI_FWD")
    ipt("-t", "nat", "-F", "WIFI_PORTAL"); ipt("-t", "nat", "-X", "WIFI_PORTAL")
    ipt("-F", "WIFI_FWD"); ipt("-X", "WIFI_FWD")

def liberar_mac(mac):
    if ipt("-t", "nat", "-C", "WIFI_PORTAL", "-m", "mac", "--mac-source", mac, "-j", "RETURN").returncode != 0:
        ipt("-t", "nat", "-I", "WIFI_PORTAL", "1", "-m", "mac", "--mac-source", mac, "-j", "RETURN")
    if ipt("-C", "WIFI_FWD", "-m", "mac", "--mac-source", mac, "-j", "ACCEPT").returncode != 0:
        ipt("-I", "WIFI_FWD", "1", "-m", "mac", "--mac-source", mac, "-j", "ACCEPT")

def bloquear_mac(mac):
    ipt("-t", "nat", "-D", "WIFI_PORTAL", "-m", "mac", "--mac-source", mac, "-j", "RETURN")
    ipt("-D", "WIFI_FWD", "-m", "mac", "--mac-source", mac, "-j", "ACCEPT")

LIBERADOS = set()

def reconciliar():
    """Aplica no firewall o que está no banco (sessões ativas + fixos - bloqueados)."""
    global LIBERADOS
    agora = int(time.time())
    with db_lock:
        conn.execute("DELETE FROM sessoes WHERE expira<=?", (agora,))
        conn.commit()
        ativos = {r[0] for r in conn.execute("SELECT mac FROM sessoes")}
        fixos = {r[0] for r in conn.execute("SELECT mac FROM fixos")}
        bloq = {r[0] for r in conn.execute("SELECT mac FROM bloqueados")}
    desejado = (ativos | fixos) - bloq
    for m in desejado - LIBERADOS:
        liberar_mac(m)
    for m in LIBERADOS - desejado:
        bloquear_mac(m)
    LIBERADOS = set(desejado)

def loop_reconciliar():
    while True:
        try:
            reconciliar()
        except Exception as ex:
            print("erro reconciliar:", ex)
        time.sleep(5)

# ==================================================================== regras
def mac_ok(m):
    m = (m or "").strip().lower().replace("-", ":")
    return m if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m) else None

def mac_do_ip(ip):
    r = sh(["ip", "neigh", "show", ip])
    m = re.search(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", r.stdout.lower())
    if m:
        return m.group(1)
    try:
        for linha in open("/proc/net/arp").read().splitlines()[1:]:
            c = linha.split()
            if c[0] == ip and c[3] != "00:00:00:00:00:00":
                return c[3].lower()
    except Exception:
        pass
    return None

def fmt_tempo(seg):
    seg = max(0, int(seg))
    d, h, m = seg // 86400, (seg % 86400) // 3600, (seg % 3600) // 60
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m:02d}min" if h else f"{m}min"

def normalizar_whatsapp(valor):
    """Retorna número internacional só com dígitos ou None se inválido."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    return digitos if 8 <= len(digitos) <= 15 and not digitos.startswith("0") else None

def contato_whatsapp_html(texto="Olá! Preciso de mais tempo de acesso ao Wi-Fi."):
    numero = normalizar_whatsapp(cfg("whatsapp_admin"))
    if not numero:
        return alerta("Para liberar mais tempo, entre em contato com o administrador da rede. O WhatsApp ainda não foi configurado.", "wa")
    url = "https://wa.me/" + numero + "?text=" + quote(texto, safe="")
    return ("<p class=ct><a class='btn pri' target=_blank rel='noopener noreferrer' href='" + url + "'>"
            "Falar com o administrador no WhatsApp</a></p>")

def conceder_acesso_inicial(mac, ip=""):
    """Concede uma única sessão inicial de 5 min por endereço MAC, de forma persistente."""
    mac = mac_ok(mac)
    if not mac or bloqueado(mac):
        return False, "Dispositivo inválido ou bloqueado."
    agora = int(time.time())
    with db_lock:
        if conn.execute("SELECT 1 FROM acessos_iniciais WHERE mac=?", (mac,)).fetchone():
            return False, "O acesso inicial de 5 minutos já foi utilizado neste dispositivo."
        ativo = conn.execute("SELECT expira FROM sessoes WHERE mac=?", (mac,)).fetchone()
        if ativo and ativo[0] > agora:
            return False, "Este dispositivo já possui uma sessão ativa."
        conn.execute("INSERT INTO acessos_iniciais(mac,concedido_em) VALUES(?,?)", (mac, agora))
        conn.execute("REPLACE INTO sessoes(mac,ip,expira) VALUES(?,?,?)", (mac, ip or "", agora + 5 * 60))
        conn.commit()
    return True, "Acesso inicial de 5 minutos liberado."

def fmt_dur(mins):
    mins = int(mins or 0)
    if mins < 60:
        return f"{mins} min"
    if mins % 1440 == 0:
        d = mins // 1440
        return f"{d} dia{'s' if d > 1 else ''}"
    if mins % 60 == 0:
        h = mins // 60
        return f"{h} hora{'s' if h > 1 else ''}"
    return fmt_tempo(mins * 60)

def reais(v):
    return "R$ " + f"{float(v or 0):.2f}".replace(".", ",")

def bloqueado(mac):
    return bool(mac) and bool(q("SELECT 1 FROM bloqueados WHERE mac=?", mac))

def sessao_ativa(mac):
    r = q("SELECT expira FROM sessoes WHERE mac=? AND expira>?", mac, int(time.time()))
    return r[0][0] if r else 0

def add_tempo(mac, minutos, ip=""):
    """Soma (ou subtrai, se negativo) minutos. Retorna o novo vencimento (0 = sem sessão)."""
    agora = int(time.time())
    with db_lock:
        row = conn.execute("SELECT expira,ip FROM sessoes WHERE mac=?", (mac,)).fetchone()
        base = row[0] if row and row[0] > agora else agora
        novo = base + int(minutos) * 60
        if novo <= agora:
            conn.execute("DELETE FROM sessoes WHERE mac=?", (mac,))
            novo = 0
        else:
            conn.execute("REPLACE INTO sessoes VALUES(?,?,?)", (mac, ip or (row[1] if row else ""), novo))
        conn.commit()
    return novo

def metodos_ativos():
    m = []
    if cfg("metodo_mp") == "1" and cfg("mp_token"):
        m.append(("mp", "Pix automático", "Aprovação imediata"))
    if cfg("metodo_pix_manual") == "1" and cfg("pix_chave"):
        m.append(("pix", "Pix com confirmação", "Liberado após conferência"))
    return m

def registrar_pagamento(pid, mac, plano, metodo):
    x("REPLACE INTO pagamentos(id,mac,status,plano_nome,minutos,metodo,valor,criado) VALUES(?,?,?,?,?,?,?,?)",
      pid, mac, "pending", plano[1], plano[2], metodo, plano[3], int(time.time()))

def aprovar_pagamento(pid):
    with db_lock:
        r = conn.execute("SELECT mac,minutos,status FROM pagamentos WHERE id=?", (pid,)).fetchone()
        if not r or r[2] == "paid":
            return False
        conn.execute("UPDATE pagamentos SET status='paid', pago_em=? WHERE id=?", (int(time.time()), pid))
        conn.commit()
    if not bloqueado(r[0]):
        add_tempo(r[0], r[1] or 0)
    return True

def usar_voucher(codigo, mac):
    codigo = re.sub(r"[^A-Z0-9]", "", (codigo or "").upper())
    with db_lock:
        r = conn.execute("SELECT minutos,usado_por FROM vouchers WHERE codigo=?", (codigo,)).fetchone()
        if not r or r[1]:
            return 0
        conn.execute("UPDATE vouchers SET usado_por=?, usado_em=? WHERE codigo=?", (mac, int(time.time()), codigo))
        conn.commit()
    add_tempo(mac, r[0])
    return r[0]

def gerar_vouchers(qtd, minutos):
    alfa = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codigos = []
    for _ in range(qtd):
        c = "".join(secrets.choice(alfa) for _ in range(8))
        x("INSERT OR IGNORE INTO vouchers VALUES(?,?,?,NULL,NULL)", c, minutos, int(time.time()))
        codigos.append(c)
    return codigos

def criar_pix_mp(valor, desc):
    corpo = json.dumps({"transaction_amount": float(valor), "description": desc,
                        "payment_method_id": "pix",
                        "payer": {"email": "cliente@wifi.local"}}).encode()
    req = urllib.request.Request(
        "https://api.mercadopago.com/v1/payments", data=corpo, method="POST",
        headers={"Authorization": "Bearer " + cfg("mp_token"), "Content-Type": "application/json",
                 "X-Idempotency-Key": str(uuid.uuid4())})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    return str(d["id"]), d["point_of_interaction"]["transaction_data"]["qr_code"]

def pago_mp(pid):
    req = urllib.request.Request("https://api.mercadopago.com/v1/payments/" + pid,
                                 headers={"Authorization": "Bearer " + cfg("mp_token")})
    return json.load(urllib.request.urlopen(req, timeout=20)).get("status") == "approved"

# ============================================================== atualização
REINICIAR_PENDENTE = False   # sinaliza ao menu do terminal que deve reiniciar
SERVIDOR_AQUI = False        # True quando o servidor roda neste mesmo processo

def vtuple(v):
    try:
        return tuple(int(p) for p in str(v).strip().split("."))
    except Exception:
        return (0,)

def url_download(u):
    """Valida a URL da atualização (https; http só em localhost)."""
    p = urlparse((u or "").strip())
    host = p.hostname or ""
    if not (p.scheme == "https" or (p.scheme == "http" and host in ("127.0.0.1", "localhost"))):
        raise ValueError("Use um link https:// para a atualização.")
    if host.endswith("dropbox.com"):
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "dl"] + [("dl", "1")]
        p = p._replace(query=urlencode(qs))
    return urlunparse(p)

def baixar(u):
    req = urllib.request.Request(url_download(u), headers={"User-Agent": "wifi-portal-updater"})
    dados = urllib.request.urlopen(req, timeout=30).read(2_000_001)
    if len(dados) > 2_000_000:
        raise ValueError("Arquivo grande demais.")
    return dados

def analisar(dados):
    """Confere se o download é mesmo este programa. Retorna (texto, versão)."""
    try:
        texto = dados.decode("utf-8")
        ast.parse(texto)
    except Exception:
        raise ValueError("O link não entregou um script Python válido (confira se é o arquivo e não uma página).")
    if f'ID_PROGRAMA = "{ID_PROGRAMA}"' not in texto:
        raise ValueError("O arquivo baixado não é deste programa.")
    m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', texto, re.M)
    if not m:
        raise ValueError("O arquivo baixado não informa a versão.")
    return texto, m.group(1)

def verificar_atualizacao():
    """Consulta o link e guarda a versão remota. Retorna a versão disponível."""
    try:
        _, nova = analisar(baixar(cfg("url_atualizacao")))
    except Exception as ex:
        cfg_set("upd_erro", str(ex)[:160]); cfg_set("upd_ultima", int(time.time()))
        raise
    cfg_set("upd_versao_remota", nova); cfg_set("upd_ultima", int(time.time())); cfg_set("upd_erro", "")
    return nova

def atualizacao_disponivel():
    r = cfg("upd_versao_remota")
    return r if r and vtuple(r) > vtuple(VERSION) else ""

def backups():
    return sorted(glob.glob(ARQUIVO + ".bak-*"), key=os.path.getmtime, reverse=True)

def atualizar(forcar=False):
    """Baixa, valida, faz backup e substitui o programa. Retorna (ok, mensagem)."""
    try:
        texto, nova = analisar(baixar(cfg("url_atualizacao")))
        cfg_set("upd_versao_remota", nova)
        if vtuple(nova) < vtuple(VERSION) and not forcar:
            return False, f"A versão remota ({nova}) é mais antiga que a instalada ({VERSION}); atualização recusada."
        try:
            with open(ARQUIVO, encoding="utf-8") as atual:
                mesmo_conteudo = atual.read() == texto
        except OSError:
            mesmo_conteudo = False
        if mesmo_conteudo and not forcar:
            return False, f"Você já está na versão mais recente ({VERSION})."
        return _instalar_texto(texto, nova)
    except Exception as ex:
        return False, "Falha na atualização: " + str(ex)[:160]

def _instalar_texto(texto, nova):
    """Instala apenas conteúdo já validado, guardando até três backups recentes."""
    tmp = ARQUIVO + ".novo"
    try:
        shutil.copy2(ARQUIVO, f"{ARQUIVO}.bak-{VERSION}")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, os.stat(ARQUIVO).st_mode)
        os.replace(tmp, ARQUIVO)
        for velho in backups()[3:]:
            try:
                os.remove(velho)
            except OSError:
                pass
        return True, f"Atualizado de {VERSION} para {nova}; backup salvo."
    except Exception as ex:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, "Falha ao instalar a atualização: " + str(ex)[:160]

def atualizar_automaticamente():
    """Baixa e instala o código do link confiável quando o conteúdo remoto mudar."""
    agora = int(time.time())
    try:
        texto, nova = analisar(baixar(cfg("url_atualizacao")))
        cfg_set("upd_versao_remota", nova)
        if vtuple(nova) < vtuple(VERSION):
            msg = f"Atualização automática recusada: versão remota {nova} é mais antiga que {VERSION}."
            cfg_set("upd_erro", msg[:160]); cfg_set("upd_ultima", agora)
            return False, msg
        try:
            with open(ARQUIVO, encoding="utf-8") as atual:
                mesmo_conteudo = atual.read() == texto
        except OSError:
            mesmo_conteudo = False
        if mesmo_conteudo:
            cfg_set("upd_erro", ""); cfg_set("upd_ultima", agora)
            return False, f"Portal atualizado ({VERSION}); nenhuma alteração nova."
        ok, msg = _instalar_texto(texto, nova)
        cfg_set("upd_ultima", agora)
        cfg_set("upd_erro", "" if ok else msg[:160])
        return ok, msg
    except Exception as ex:
        msg = "Falha na atualização automática: " + str(ex)[:140]
        cfg_set("upd_erro", msg[:160]); cfg_set("upd_ultima", agora)
        return False, msg

def reverter():
    b = backups()
    if not b:
        return False, "Não há versão anterior guardada."
    try:
        with open(b[0], encoding="utf-8") as f:
            analisar(f.read().encode())
        shutil.copy2(ARQUIVO, ARQUIVO + ".desfeita")
        shutil.copy2(b[0], ARQUIVO)
        return True, "Versão anterior restaurada (" + os.path.basename(b[0]).split(".bak-")[-1] + ")."
    except Exception as ex:
        return False, "Falha ao reverter: " + str(ex)[:160]

def reiniciar():
    os.execv(sys.executable, [sys.executable] + sys.argv)

def reiniciar_em(seg=1.5):
    threading.Timer(seg, reiniciar).start()

def loop_atualizacao():
    time.sleep(90)
    while True:
        if cfg("upd_auto") == "1":
            atualizado, mensagem = atualizar_automaticamente()
            if atualizado:
                print(mensagem, "Reiniciando para carregar a nova versão…")
                reiniciar()
            elif cfg("upd_erro"):
                print(cfg("upd_erro"))
        time.sleep(15 * 60)

# ===================================================================== HTML
ICONES = {
    "painel": '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    "clientes": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "planos": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "pagamentos": '<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>',
    "vouchers": '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
    "bloqueios": '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
    "sistema": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "sair": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
    "alerta": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "ok": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    "baixar": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
}

def ico(n):
    return ('<svg class=i viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round '
            f'stroke-linejoin=round>{ICONES[n]}</svg>')

CSS = """<meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><meta name=color-scheme content='dark light'>
<style>
:root{--bg:#0a1020;--pn:#111a30;--pn2:#17223d;--bd:#22304f;--tx:#e8edf7;--mu:#8fa0bf;--pri:#6366f1;--pt:#a5b4fc;--ok:#22c55e;--wa:#f59e0b;--ba:#ef4444;--sh:0 1px 2px rgba(0,0,0,.3),0 10px 28px rgba(0,0,0,.18)}
@media(prefers-color-scheme:light){:root{--bg:#f3f5fb;--pn:#fff;--pn2:#f1f4fa;--bd:#e3e8f2;--tx:#0f172a;--mu:#62718c;--pt:#4f46e5;--sh:0 1px 2px rgba(15,23,42,.06),0 10px 28px rgba(15,23,42,.07)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
a{color:var(--pt);text-decoration:none}h1,h2,h3{margin:0}
.i{width:18px;height:18px;flex:none}
.app{display:flex;min-height:100vh}
.side{width:240px;flex:none;background:var(--pn);border-right:1px solid var(--bd);padding:18px 14px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:3px}
.brand{display:flex;align-items:center;gap:11px;font-weight:700;font-size:16px;padding:2px 8px 20px}
.logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,var(--pri),#06b6d4);display:grid;place-items:center;color:#fff;flex:none}
.logo .i{width:20px;height:20px}
.brand small{display:block;font-weight:500;color:var(--mu);font-size:12px;margin-top:-2px}
.nv{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:10px;color:var(--mu);font-weight:550}
.nv:hover{background:var(--pn2);color:var(--tx)}.nv.on{background:rgba(99,102,241,.16);color:var(--pt)}
.cnt{margin-left:auto;background:var(--wa);color:#111;border-radius:99px;font-size:11px;font-weight:700;padding:0 7px}
.dot{margin-left:auto;width:8px;height:8px;border-radius:50%;background:var(--ok)}
.sp{flex:1}.ver{color:var(--mu);font-size:12px;padding:6px 12px}
.main{flex:1;min-width:0;padding:30px 34px 60px;max-width:1160px}
.ph{margin-bottom:22px}.ph h1{font-size:25px;letter-spacing:-.015em}.ph p{margin:4px 0 0;color:var(--mu)}
.card{background:var(--pn);border:1px solid var(--bd);border-radius:16px;padding:20px;margin-bottom:16px;box-shadow:var(--sh)}
.card>h2{font-size:16px;margin-bottom:3px}.sub{color:var(--mu);font-size:13px;margin:0 0 16px}
.grid{display:grid;gap:16px}.g4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-bottom:16px}.g2{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.grid>.card{margin:0}
.stat{background:var(--pn);border:1px solid var(--bd);border-radius:16px;padding:16px 18px;box-shadow:var(--sh)}
.stat .l{color:var(--mu);font-size:13px;font-weight:550}.stat .v{font-size:27px;font-weight:700;margin-top:2px;letter-spacing:-.02em}.stat .s{font-size:12px;color:var(--mu)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--bd);background:var(--pn2);color:var(--tx);padding:9px 15px;border-radius:10px;font:inherit;font-weight:600;cursor:pointer;line-height:1.2}
.btn:hover{filter:brightness(1.12)}.btn.pri{background:var(--pri);border-color:var(--pri);color:#fff}.btn.ok{background:var(--ok);border-color:var(--ok);color:#fff}
.btn.dan{background:transparent;color:var(--ba);border-color:rgba(239,68,68,.5)}.btn.sm{padding:5px 11px;font-size:13px;border-radius:8px}.btn.w{width:100%;padding:14px;font-size:16px}
input:not([type=checkbox]):not([type=radio]),select{background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:10px;padding:9px 12px;font:inherit;width:100%;min-width:0}
input:focus,select:focus{outline:2px solid var(--pri);outline-offset:1px}input[readonly]{opacity:.65}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--pri)}
.f{display:grid;gap:14px}.f label{display:grid;gap:5px;font-weight:600;font-size:13px}.f label small{font-weight:400;color:var(--mu)}
.f label.cb{display:flex;align-items:center;gap:10px;font-weight:500;font-size:15px}
.cols{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.inl{display:inline-flex;gap:6px;align-items:center;margin:0}.inl input{width:84px;padding:6px 9px}
.tw{overflow-x:auto;border:1px solid var(--bd);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{color:var(--mu);font-weight:650;font-size:12px;text-transform:uppercase;letter-spacing:.05em;text-align:left;padding:10px 14px;background:var(--pn2);white-space:nowrap}
td{padding:11px 14px;border-top:1px solid var(--bd);vertical-align:middle;white-space:nowrap}tr:hover td{background:rgba(127,127,127,.05)}
.b{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:650;white-space:nowrap}
.b.ok{background:rgba(34,197,94,.16);color:#22c55e}.b.wa{background:rgba(245,158,11,.17);color:#f59e0b}.b.ba{background:rgba(239,68,68,.15);color:#ef4444}.b.mu{background:rgba(143,160,191,.17);color:var(--mu)}.b.in{background:rgba(99,102,241,.17);color:var(--pt)}
.al{display:flex;gap:11px;align-items:flex-start;padding:13px 15px;border-radius:12px;margin-bottom:12px;border:1px solid}
.al .i{margin-top:2px}.al.wa{background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.45)}.al.ba{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.45)}.al.ok{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.4)}.al.in{background:rgba(99,102,241,.1);border-color:rgba(99,102,241,.45)}
td .row{flex-wrap:nowrap}td .inl input{width:70px}
.empty{color:var(--mu);text-align:center;padding:26px 10px}
.toast{position:fixed;top:16px;left:50%;transform:translateX(-50%);background:var(--pn);border:1px solid var(--bd);border-left:4px solid var(--ok);padding:12px 18px;border-radius:12px;box-shadow:var(--sh);z-index:9;transition:opacity .5s;max-width:92vw}
.bars{display:flex;align-items:flex-end;gap:10px;height:150px}
.bars .c{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;gap:5px;font-size:11px;color:var(--mu)}
.bars i{display:block;width:100%;background:linear-gradient(var(--pri),#06b6d4);border-radius:7px 7px 3px 3px;min-height:3px}
.code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:var(--pn2);padding:2px 8px;border-radius:7px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:8px 12px;font-size:14px;margin-bottom:14px}.kv dt{color:var(--mu)}.kv dd{margin:0;word-break:break-all}
.plan{border:1px solid var(--bd);border-radius:14px;padding:16px;background:var(--pn)}
.plan .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:8px}
.mth{border:1px solid var(--bd);border-radius:14px;padding:16px;margin-bottom:12px}.mth h3{font-size:15px}.mth p{margin:2px 0 12px;color:var(--mu);font-size:13px}
.login{max-width:380px;margin:12vh auto 0;padding:0 16px}.login .logo{width:52px;height:52px;border-radius:15px;margin:0 auto 14px}
.ct{text-align:center}
.cw{max-width:480px;margin:0 auto;padding:24px 16px 40px}
.hero{text-align:center;padding:8px 0 20px}.hero .logo{margin:0 auto 14px;width:58px;height:58px;border-radius:17px}.hero .logo .i{width:30px;height:30px}.hero h1{font-size:23px}.hero p{color:var(--mu);margin:4px 0 0}
.sec{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--mu);margin:20px 0 9px;font-weight:700}
.opt{display:block;position:relative;margin:0 0 9px;cursor:pointer}.opt input{position:absolute;opacity:0}
.oc{display:flex;justify-content:space-between;align-items:center;gap:10px;border:1.5px solid var(--bd);border-radius:13px;padding:13px 15px;background:var(--pn2)}
.opt input:checked+.oc{border-color:var(--pri);background:rgba(99,102,241,.13)}.opt input:focus-visible+.oc{outline:2px solid var(--pri)}
.oc b{font-size:16px}.oc small{display:block;color:var(--mu)}.oc .pr{font-size:18px;font-weight:700;white-space:nowrap}
details summary{cursor:pointer;color:var(--mu);margin-top:20px;text-align:center}
.big{font-size:34px;font-weight:800;letter-spacing:-.02em;margin:4px 0 10px}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--bd);border-top-color:var(--pri);border-radius:50%;animation:g 1s linear infinite;vertical-align:-2px;margin-right:8px}@keyframes g{to{transform:rotate(360deg)}}
textarea{width:100%;height:96px;font:12px ui-monospace,monospace;background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:10px;padding:10px}
@media(max-width:860px){.app{display:block}.side{position:static;width:auto;height:auto;flex-direction:row;overflow-x:auto;padding:8px 10px;border-right:0;border-bottom:1px solid var(--bd);gap:2px}
.brand,.sp,.ver{display:none}.nv{white-space:nowrap;padding:9px 12px}.nv .t{display:inline}.main{padding:20px 14px 50px}.g2{grid-template-columns:1fr}.kv{grid-template-columns:1fr}}
</style>"""

def pagina(titulo, corpo):
    return f"<!doctype html><html lang=pt-BR><head><title>{e(titulo)}</title>{CSS}</head><body>{corpo}</body></html>"

def badge(t, k="mu"):
    return f"<span class='b {k}'>{e(str(t))}</span>"

def alerta(txt, k="wa", icone="alerta"):
    return f"<div class='al {k}'>{ico(icone)}<div>{txt}</div></div>"

def stat(rotulo, valor, sub=""):
    return f"<div class=stat><div class=l>{rotulo}</div><div class=v>{valor}</div><div class=s>{sub}&nbsp;</div></div>"

def tabela(cab, linhas, vazio="Nada por aqui ainda."):
    if not linhas:
        return f"<div class=empty>{vazio}</div>"
    return ("<div class=tw><table><tr>" + "".join(f"<th>{c}</th>" for c in cab) + "</tr>"
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in l) + "</tr>" for l in linhas) + "</table></div>")

def ck(nome, marcado):
    return f"<input type=checkbox name={nome} value=1 {'checked' if marcado else ''}>"

STATUS = {"paid": ("Pago", "ok"), "pending": ("Pendente", "wa"), "analise": ("Aviso enviado", "wa"), "rejeitado": ("Rejeitado", "ba")}
METODOS_NOME = {"mp": "Pix automático", "pix": "Pix manual", "teste": "Teste"}

# --------------------------------------------------------------- página cliente
def pagina_cliente(mac, msg=""):
    nome = e(cfg("nome_rede"))
    topo = f"<div class=hero><div class=logo>{ico('wifi')}</div><h1>{nome}</h1><p>Gerenciamento de acesso à rede</p></div>"
    aviso = alerta(e(msg), "in", "alerta") if msg else ""
    rodape = ("<p class=ct style='margin-top:26px'><small><a href=/admin>Área do administrador</a></small></p>"
              "<p class=ct><small style='color:var(--mu)'>Ideia e desenvolvimento: khallendev</small></p>")
    if bloqueado(mac):
        return pagina(nome, f"<div class=cw>{topo}{alerta('Este dispositivo está bloqueado. Fale com o administrador.', 'ba')}{contato_whatsapp_html()}{rodape}</div>")
    ativo = ""
    exp = sessao_ativa(mac) if mac else 0
    if exp:
        ativo = alerta(f"<b>Acesso ativo.</b> Tempo restante: <b>{fmt_tempo(exp - time.time())}</b>.<br>"
                       "<small>Para solicitar mais tempo, fale com o administrador da rede.</small>", "ok", "ok")
    corpo = topo + aviso + ativo
    mp_ligado = cfg("metodo_mp") == "1" and bool(cfg("mp_token"))
    ja_usou_inicial = bool(mac and q("SELECT 1 FROM acessos_iniciais WHERE mac=?", mac))
    if not exp and mac and not ja_usou_inicial:
        corpo += ("<div class=card><h2>Acesso inicial gratuito</h2><p class=sub>Este dispositivo pode iniciar uma sessão única de 5 minutos.</p>"
                  "<form method=post action=/inicio><button class='btn pri w'>Iniciar acesso de 5 minutos</button></form></div>")
    elif not exp and ja_usou_inicial:
        corpo += alerta("O acesso inicial de 5 minutos já foi utilizado neste dispositivo. Para liberar mais tempo, fale com o administrador.", "wa")
    corpo += contato_whatsapp_html()
    # A compra de planos só aparece quando o administrador habilita o Mercado Pago.
    if mp_ligado:
        planos = q("SELECT id,nome,minutos,preco FROM planos WHERE ativo=1 ORDER BY minutos")
        metodos = metodos_ativos()
        if planos and metodos:
            corpo += "<form method=post action=/pagar><div class=sec>Planos disponíveis</div>"
            for i, (pid, pn, pm, pp) in enumerate(planos):
                corpo += (f"<label class=opt><input type=radio name=plano value={pid} {'checked' if i == 0 else ''}>"
                          f"<span class=oc><span><b>{e(pn)}</b><small>{fmt_dur(pm)} de acesso</small></span><span class=pr>{reais(pp)}</span></span></label>")
            corpo += "<div class=sec>Forma de pagamento</div>"
            for i, (mid, mn, md) in enumerate(metodos):
                corpo += (f"<label class=opt><input type=radio name=metodo value={mid} {'checked' if i == 0 else ''}>"
                          f"<span class=oc><span><b>{e(mn)}</b><small>{e(md)}</small></span></span></label>")
            corpo += "<button class='btn pri w' style='margin-top:12px'>Continuar</button></form>"
        if cfg("metodo_voucher") == "1":
            corpo += ("<details><summary>Tenho um voucher</summary><form method=post action=/voucher class=row style='margin-top:12px;flex-wrap:nowrap'>"
                      "<input name=codigo placeholder='CÓDIGO' autocomplete=off autocapitalize=characters><button class=btn>Usar</button></form></details>")
    elif not exp:
        corpo += alerta("O pagamento online está desativado. Para obter mais tempo, solicite a liberação ao administrador.", "in")
    return pagina(nome, f"<div class=cw>{corpo}{rodape}</div>")

def pagina_pagamento(pid, valor, extra):
    script = ("<script>var ok=0;function chk(){if(ok)return;fetch('/status?id=" + pid + "').then(r=>r.json()).then(d=>{if(d.ok){ok=1;"
              "document.getElementById('s').innerHTML='<b style=color:var(--ok)>✅ Pagamento confirmado! Acesso liberado, pode navegar.</b>'}})}"
              "setInterval(chk,3000)</script>")
    return pagina("Pagamento", "<div class=cw><div class=card style='text-align:center'><div class=sec style='margin-top:0'>Valor a pagar</div>"
                  f"<div class=big>{reais(valor)}</div>{extra}"
                  "<p id=s style='margin-top:18px;color:var(--mu)'><span class=spin></span>Aguardando pagamento…</p></div>"
                  "<p class=ct><a href=/>← voltar</a></p></div>" + script)

# -------------------------------------------------------------------- servidor
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def resp(self, corpo, code=200, tipo="text/html; charset=utf-8", cab=()):
        b = corpo.encode()
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in cab:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def redir(self, url, cab=()):
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        for k, v in cab:
            self.send_header(k, v)
        self.end_headers()

    def ler_form(self):
        n = min(int(self.headers.get("Content-Length") or 0), 20000)
        dados = parse_qs(self.rfile.read(n).decode("utf-8", "ignore"), keep_blank_values=True)
        return {k: v[0] for k, v in dados.items()}

    def local(self):
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1") or ip == self.connection.getsockname()[0]

    def sessao_admin(self):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == "adm" and v in SESSOES_ADMIN:
                s = SESSOES_ADMIN[v]
                if s["exp"] > time.time():
                    s["exp"] = time.time() + 3600
                    s["tok"] = v
                    return s
                SESSOES_ADMIN.pop(v, None)
        return None

    def do_GET(self):
        self.rotear("GET")

    def do_POST(self):
        self.rotear("POST")

    def rotear(self, metodo):
        try:
            u = urlparse(self.path)
            rota = u.path.rstrip("/") or "/"
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            form = self.ler_form() if metodo == "POST" else {}
            if rota.startswith("/admin"):
                return self.admin(metodo, rota, qs, form)
            return self.cliente(metodo, rota, qs, form)
        except Exception as ex:
            print("erro:", repr(ex))
            try:
                self.resp("Erro interno.", 500)
            except Exception:
                pass

    # ---------------------------------------------------------- lado do cliente
    def cliente(self, metodo, rota, qs, form):
        ip = self.client_address[0]
        mac = mac_do_ip(ip)

        if rota == "/status":
            pid = qs.get("id", "")
            r = q("SELECT status,metodo FROM pagamentos WHERE id=?", pid)
            ok = False
            if r:
                if r[0][0] != "paid" and r[0][1] == "mp":
                    try:
                        if pago_mp(pid):
                            aprovar_pagamento(pid)
                    except Exception:
                        pass
                ok = q("SELECT status FROM pagamentos WHERE id=?", pid)[0][0] == "paid"
            return self.resp(json.dumps({"ok": ok}), tipo="application/json")

        if metodo == "POST" and rota == "/inicio":
            if not mac:
                return self.resp(pagina_cliente(mac, "Não foi possível identificar seu dispositivo. Reconecte ao Wi-Fi."), 400)
            ok, mensagem = conceder_acesso_inicial(mac, ip)
            if ok:
                reconciliar()
            return self.resp(pagina_cliente(mac, mensagem))

        if metodo == "POST" and rota == "/paguei":
            x("UPDATE pagamentos SET status='analise' WHERE id=? AND metodo='pix' AND status='pending'", form.get("id", ""))
            return self.resp("{}", tipo="application/json")

        if metodo == "POST" and rota in ("/pagar", "/voucher"):
            if cfg("metodo_mp") != "1" or not cfg("mp_token"):
                return self.resp(pagina_cliente(mac, "Pagamentos online desativados. Fale com o administrador para solicitar mais tempo."), 403)
            if rota == "/voucher" and cfg("metodo_voucher") != "1":
                return self.resp(pagina_cliente(mac, "O uso de vouchers está desativado."), 403)
            if not mac:
                return self.resp(pagina_cliente(mac, "Não foi possível identificar seu dispositivo. Reconecte ao Wi-Fi."), 400)
            if bloqueado(mac):
                return self.resp(pagina_cliente(mac))
            if rota == "/voucher":
                if limitado("v:" + ip):
                    return self.resp(pagina_cliente(mac, "Muitas tentativas. Aguarde alguns minutos."), 429)
                mins = usar_voucher(form.get("codigo", ""), mac)
                if not mins:
                    falhou("v:" + ip, limite=8)
                    return self.resp(pagina_cliente(mac, "Voucher inválido ou já utilizado."))
                return self.resp(pagina_cliente(mac, f"Voucher aceito! +{fmt_dur(mins)} liberados."))
            plano = q("SELECT id,nome,minutos,preco FROM planos WHERE id=? AND ativo=1", form.get("plano", "0"))
            mid = form.get("metodo", "")
            if not plano or mid not in {m[0] for m in metodos_ativos()}:
                return self.resp(pagina_cliente(mac, "Escolha um plano e uma forma de pagamento válidos."))
            plano = plano[0]
            try:
                if mid == "mp":
                    pid, codigo = criar_pix_mp(plano[3], f"{cfg('nome_rede')} - {plano[1]}")
                    registrar_pagamento(pid, mac, plano, "mp")
                    extra = (f"<p style='color:var(--mu)'>Copie o código Pix e pague no app do seu banco:</p><textarea id=t readonly>{e(codigo)}</textarea>"
                             "<button class='btn pri w' style='margin-top:10px' onclick=\"t.select();document.execCommand('copy');this.textContent='Copiado ✓'\">Copiar código Pix</button>")
                elif mid == "pix":
                    pid = uuid.uuid4().hex[:8]
                    registrar_pagamento(pid, mac, plano, "pix")
                    extra = (f"<p style='color:var(--mu)'>Faça um Pix para a chave:</p><p><span class=code style='font-size:16px'>{e(cfg('pix_chave'))}</span><br>"
                             f"<small style='color:var(--mu)'>{e(cfg('pix_nome'))}</small></p><p>Referência: <b>{pid}</b></p>"
                             f"<button class='btn pri w' onclick=\"fetch('/paguei',{{method:'POST',body:'id={pid}'}});this.textContent='Aviso enviado ✓'\">Já fiz o pagamento</button>"
                             "<p><small style='color:var(--mu)'>O administrador confere e libera seu acesso.</small></p>")
                else:
                    pid = uuid.uuid4().hex[:8]
                    registrar_pagamento(pid, mac, plano, "teste")
                    extra = (f"<button class='btn pri w' onclick=\"fetch('/simular',{{method:'POST',body:'id={pid}'}}).then(chk)\">"
                             "Simular pagamento (modo teste)</button>")
            except Exception as ex:
                return self.resp(pagina_cliente(mac, "Erro ao gerar pagamento: " + str(ex)[:120]), 502)
            return self.resp(pagina_pagamento(pid, plano[3], extra))

        self.resp(pagina_cliente(mac))

    # -------------------------------------------------------------- lado admin
    def admin(self, metodo, rota, qs, form):
        if cfg("admin_somente_local") == "1" and not self.local():
            return self.resp(pagina("Acesso negado", "<div class=login>" + alerta("O painel só pode ser aberto no aparelho do administrador.", "ba") + "</div>"), 403)
        ip = self.client_address[0]
        if rota == "/admin/reiniciando":
            return self.resp(pagina_reiniciando())
        if rota == "/admin/login" and metodo == "POST":
            if limitado("a:" + ip):
                return self.resp(pagina_login("Muitas tentativas. Aguarde 5 minutos."), 429)
            if confere_senha(form.get("senha", "")):
                tok = secrets.token_urlsafe(24)
                SESSOES_ADMIN[tok] = {"exp": time.time() + 3600, "csrf": secrets.token_urlsafe(12)}
                return self.redir("/admin", [("Set-Cookie", f"adm={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600")])
            falhou("a:" + ip)
            time.sleep(1)
            return self.resp(pagina_login("Senha incorreta."), 401)
        ses = self.sessao_admin()
        if not ses:
            return self.resp(pagina_login(qs.get("m", "")))
        if rota == "/admin/sair":
            SESSOES_ADMIN.pop(ses["tok"], None)
            return self.redir("/admin", [("Set-Cookie", "adm=; Max-Age=0; Path=/")])
        if metodo == "POST":
            if not hmac.compare_digest(form.get("_c", ""), ses["csrf"]):
                return self.resp("Requisição inválida.", 403)
            fn = ACOES.get(rota)
            if not fn:
                return self.resp("Ação desconhecida.", 404)
            form["_local"] = "1" if self.local() else "0"   # sempre definido pelo servidor
            destino, msg = fn(form)
            return self.redir(destino + "?m=" + quote(msg))
        pg = PAGINAS.get(rota)
        if not pg:
            return self.resp("Página não encontrada.", 404)
        chave, fn = pg
        titulo, sub, corpo = fn(ses, self.local())
        self.resp(layout_admin(chave, titulo, sub, corpo, qs.get("m", "")))

SESSOES_ADMIN = {}

def pagina_login(msg=""):
    av = alerta(e(msg), "ba") if msg else ""
    return pagina("Administração", f"<div class=login><div class=ct><div class=logo>{ico('wifi')}</div><h1>Administração</h1>"
                  f"<p style='color:var(--mu);margin:4px 0 20px'>{e(cfg('nome_rede'))}</p></div><div class=card>{av}"
                  "<form method=post action=/admin/login class=f><label>Senha<input type=password name=senha autofocus autocomplete=current-password></label>"
                  "<button class='btn pri w'>Entrar</button></form></div><p class=ct><small><a href=/>← voltar ao portal</a></small></p>"
                  "<p class=ct><small style='color:var(--mu)'>Ideia e desenvolvimento: khallendev</small></p></div>")

def pagina_reiniciando():
    return pagina("Reiniciando", f"<div class=login><div class='card ct'><div class=logo style='margin:0 auto 14px'>{ico('baixar')}</div>"
                  "<h2>Reiniciando o portal…</h2><p style='color:var(--mu)'><span class=spin></span>Aguarde alguns segundos. Você será levado ao painel.</p></div></div>"
                  "<script>async function t(){try{let r=await fetch('/admin',{cache:'no-store'});if(r.ok){location.href='/admin';return}}catch(e){}setTimeout(t,1500)}setTimeout(t,4500)</script>")

NAV = [("painel", "/admin", "Painel"), ("clientes", "/admin/clientes", "Clientes"), ("planos", "/admin/planos", "Planos"),
       ("pagamentos", "/admin/pagamentos", "Pagamentos"), ("vouchers", "/admin/vouchers", "Vouchers"),
       ("bloqueios", "/admin/bloqueios", "Bloqueios"), ("sistema", "/admin/sistema", "Sistema")]

def layout_admin(ativa, titulo, sub, corpo, msg=""):
    pend = q("SELECT COUNT(*) FROM pagamentos WHERE metodo='pix' AND status IN ('pending','analise')")[0][0]
    links = ""
    for chave, url, nome in NAV:
        extra = ""
        if chave == "pagamentos" and pend:
            extra = f"<span class=cnt>{pend}</span>"
        if chave == "sistema" and atualizacao_disponivel():
            extra = "<span class=dot title='Atualização disponível'></span>"
        links += f"<a class='nv{' on' if chave == ativa else ''}' href='{url}'>{ico(chave)}<span class=t>{nome}</span>{extra}</a>"
    lateral = (f"<nav class=side><div class=brand><div class=logo>{ico('wifi')}</div><div>Portal Wi-Fi<small>{e(cfg('nome_rede'))}</small></div></div>"
               f"{links}<div class=sp></div><a class=nv href=/admin/sair>{ico('sair')}<span class=t>Sair</span></a>"
               f"<div class=ver>v{VERSION} · idealizado por khallendev</div></nav>")
    toast = ""
    if msg:
        toast = (f"<div class=toast id=tt>{e(msg)}</div><script>setTimeout(function(){{var t=document.getElementById('tt');t.style.opacity=0;"
                 "setTimeout(function(){t.remove()},600)},4000)</script>")
    return pagina("Admin · " + titulo, f"<div class=app>{lateral}<main class=main>{toast}"
                  f"<div class=ph><h1>{e(titulo)}</h1><p>{e(sub)}</p></div>{corpo}</main></div>")

def F(ses, acao, inner="", btn="Salvar", cls="", conf="", bloco=False):
    on = f" onsubmit=\"return confirm('{e(conf)}')\"" if conf else ""
    c = "f" if bloco else "inl"
    botao = f"<button class='btn {cls}'>{btn}</button>"
    return (f"<form method=post action='{acao}' class={c}{on}><input type=hidden name=_c value='{ses['csrf']}'>"
            f"{inner}{botao}</form>")

def inicio_do_dia():
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))

def receita(ini, fim=None):
    sql = "SELECT COALESCE(SUM(valor),0) FROM pagamentos WHERE status='paid' AND metodo IN ('mp','pix') AND pago_em>=?"
    p = [ini]
    if fim:
        sql += " AND pago_em<?"; p.append(fim)
    return q(sql, *p)[0][0]

def hdr_fmt(t):
    return time.strftime("%d/%m %H:%M", time.localtime(t or 0))

# --- páginas: cada uma devolve (titulo, subtitulo, html)
def pg_painel(ses, local):
    agora = int(time.time())
    ativos = q("SELECT COUNT(*) FROM sessoes WHERE expira>?", agora)[0][0]
    pend = q("SELECT COUNT(*) FROM pagamentos WHERE metodo='pix' AND status IN ('pending','analise')")[0][0]
    ini = inicio_do_dia()
    hoje, total = receita(ini), receita(0)
    sem = receita(ini - 6 * 86400)
    av = ""
    nova = atualizacao_disponivel()
    if nova:
        av += alerta(f"<b>Nova versão {e(nova)} disponível</b> (você usa a {VERSION}). <a href=/admin/sistema>Ver atualização →</a>", "in", "baixar")
    if cfg("senha_padrao") == "1":
        av += alerta("Você ainda usa a <b>senha padrão</b>. <a href=/admin/sistema>Altere agora →</a>", "wa")
    if cfg("metodo_teste") == "1":
        av += alerta("<b>Modo teste ligado:</b> qualquer cliente consegue acesso grátis. <a href=/admin/pagamentos>Desligar →</a>", "wa")
    if pend:
        av += alerta(f"<b>{pend}</b> pagamento(s) Pix aguardando sua confirmação. <a href=/admin/pagamentos>Conferir →</a>", "in")
    stats = ("<div class='grid g4'>" + stat("Clientes conectados", ativos, "agora")
             + stat("Receita hoje", reais(hoje), "Pix confirmados") + stat("Últimos 7 dias", reais(sem), "")
             + stat("Receita total", reais(total), "") + "</div>")
    dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    vals = []
    for i in range(6, -1, -1):
        d0 = ini - i * 86400
        vals.append((dias[time.localtime(d0 + 3600).tm_wday], receita(d0, d0 + 86400)))
    mx = max([v for _, v in vals] + [0.01])
    barras = "".join(f"<div class=c><span>{reais(v).replace('R$ ', '') if v else ''}</span><i style='height:{max(3, int(v / mx * 100))}%'></i><span>{d}</span></div>" for d, v in vals)
    rec = []
    for t, m, mac, n, v, s in q("SELECT criado,metodo,mac,plano_nome,valor,status FROM pagamentos ORDER BY criado DESC LIMIT 6"):
        rotulo, k = STATUS.get(s, (s, "mu"))
        rec.append([hdr_fmt(t), e(METODOS_NOME.get(m, m or "")), e(n or ""), reais(v), badge(rotulo, k)])
    return ("Painel", "Visão geral da sua rede", av + stats
            + "<div class='grid g2'><div class=card><h2>Receita dos últimos 7 dias</h2><p class=sub>Pagamentos Pix confirmados por dia</p>"
            f"<div class=bars>{barras}</div></div>"
            "<div class=card><h2>Atividade recente</h2><p class=sub>Últimas tentativas de pagamento</p>"
            + tabela(["Quando", "Método", "Plano", "Valor", "Status"], rec, "Nenhum pagamento ainda.") + "</div></div>")

def pg_clientes(ses, local):
    agora = time.time()
    linhas = []
    for mac, ip, exp in q("SELECT mac,ip,expira FROM sessoes WHERE expira>? ORDER BY expira DESC", int(agora)):
        h = f"<input type=hidden name=mac value='{e(mac)}'>"
        linhas.append([f"<span class=code>{e(mac)}</span>", e(ip or "—"), badge(fmt_tempo(exp - agora), "ok"),
                       "<div class=row>" + F(ses, "/admin/clientes/ajustar", h + "<input name=min placeholder='±min'>", "Aplicar", "sm")
                       + F(ses, "/admin/clientes/encerrar", h, "Encerrar", "sm dan", "Encerrar a sessão deste cliente?")
                       + F(ses, "/admin/bloqueios/add", h, "Bloquear", "sm dan", "Bloquear este dispositivo?") + "</div>"])
    fixos = [[f"<span class=code>{e(m)}</span>", e(n or "—"),
              F(ses, "/admin/clientes/fixo_rm", f"<input type=hidden name=mac value='{e(m)}'>", "Remover", "sm dan")]
             for m, n in q("SELECT mac,nome FROM fixos")]
    dl = "<datalist id=pm>" + "".join(f"<option value={m}>" for (m,) in q("SELECT DISTINCT minutos FROM planos")) + "</datalist>"
    liberar = ("<div class=card><h2>Liberar tempo manualmente</h2><p class=sub>Some minutos a um dispositivo. Use valor negativo para remover.</p>"
               + F(ses, "/admin/clientes/ajustar", "<div class=cols><label>MAC do dispositivo<input name=mac placeholder='aa:bb:cc:dd:ee:ff'></label>"
                   f"<label>Minutos<input name=min list=pm placeholder=60></label></div>{dl}", "Liberar tempo", "pri", bloco=True) + "</div>")
    return ("Clientes", "Quem está conectado e controle de acesso",
            liberar + f"<div class=card><h2>Conectados agora</h2><p class=sub>{len(linhas)} cliente(s) com acesso ativo</p>"
            + tabela(["Dispositivo", "IP", "Restante", "Ações"], linhas, "Nenhum cliente conectado.") + "</div>"
            "<div class=card><h2>Acesso permanente</h2><p class=sub>Dispositivos que nunca pagam (o seu, de funcionários…)</p>"
            + F(ses, "/admin/clientes/fixo_add", "<div class=cols><label>MAC<input name=mac placeholder='aa:bb:cc:dd:ee:ff'></label><label>Nome<input name=nome placeholder='Meu celular'></label></div>", "Adicionar", "pri", bloco=True)
            + "<div style='margin-top:14px'>" + tabela(["Dispositivo", "Nome", ""], fixos, "Nenhum acesso permanente.") + "</div></div>")

def pg_planos(ses, local):
    cards = ""
    for pid, nome, mins, preco, ativo in q("SELECT id,nome,minutos,preco,ativo FROM planos ORDER BY minutos"):
        campos = (f"<input type=hidden name=id value={pid}><div class=cols><label>Nome<input name=nome value='{e(nome)}'></label>"
                  f"<label>Minutos<input name=minutos value={mins}></label><label>Preço (R$)<input name=preco value={preco:.2f}></label></div>"
                  f"<label class=cb>{ck('ativo', ativo)} Plano ativo (visível para clientes)</label>")
        cards += (f"<div class=plan><div class=top><div><b>{e(nome)}</b><br><small style='color:var(--mu)'>{fmt_dur(mins)} · {reais(preco)}</small></div>"
                  f"{badge('Ativo', 'ok') if ativo else badge('Inativo', 'mu')}</div>"
                  + F(ses, "/admin/planos/salvar", campos, "Salvar alterações", "pri", bloco=True)
                  + "<div style='margin-top:8px'>" + F(ses, "/admin/planos/excluir", f"<input type=hidden name=id value={pid}>", "Excluir plano", "sm dan", "Excluir este plano?") + "</div></div>")
    novo = ("<div class=card><h2>Novo plano</h2><p class=sub>Defina o nome, quanto tempo dura e o preço.</p>"
            + F(ses, "/admin/planos/novo", "<div class=cols><label>Nome<input name=nome placeholder='Diário'></label><label>Minutos<input name=minutos placeholder=1440></label>"
                "<label>Preço (R$)<input name=preco placeholder='5,00'></label></div>", "Criar plano", "pri", bloco=True) + "</div>")
    return "Planos", "Pacotes de tempo oferecidos aos clientes", novo + f"<div class='grid g2'>{cards}</div>"

def pg_pagamentos(ses, local):
    tok = cfg("mp_token")
    st = badge("Token configurado …" + tok[-4:], "ok") if tok else badge("Token não configurado", "wa")
    metodos = (
        f"<div class=mth><h3>Mercado Pago · opcional</h3><p>Quando ativado, permite comprar os planos online. Desativado: clientes recebem apenas o acesso inicial de 5 minutos e devem solicitar tempo adicional pelo WhatsApp. {st}</p>"
        f"<div class=f><label class=cb>{ck('metodo_mp', cfg('metodo_mp') == '1')} Ativar</label>"
        "<label>Access token<input type=password name=mp_token placeholder='Cole o novo token (vazio mantém o atual)' autocomplete=off></label></div></div>"
        f"<div class=mth><h3>Pix manual</h3><p>Disponível apenas quando o Mercado Pago estiver habilitado. O cliente paga na sua chave e você confirma no painel.</p>"
        f"<div class=f><label class=cb>{ck('metodo_pix_manual', cfg('metodo_pix_manual') == '1')} Ativar</label><div class=cols>"
        f"<label>Chave Pix<input name=pix_chave value='{e(cfg('pix_chave'))}'></label><label>Nome do titular<input name=pix_nome value='{e(cfg('pix_nome'))}'></label></div></div></div>"
        f"<div class=mth><h3>Vouchers</h3><p>Códigos pré-pagos, disponíveis para clientes somente quando o checkout do Mercado Pago estiver habilitado.</p>"
        f"<label class=cb style='display:flex;gap:10px;align-items:center'>{ck('metodo_voucher', cfg('metodo_voucher') == '1')} Ativar</label></div>"
        )
    pend = []
    for pid, mac, nome, valor, st_ in q("SELECT id,mac,plano_nome,valor,status FROM pagamentos WHERE metodo='pix' AND status IN ('pending','analise') ORDER BY criado DESC LIMIT 50"):
        hid = f"<input type=hidden name=id value={pid}>"
        rot, k = STATUS[st_] if st_ in STATUS else (st_, "mu")
        pend.append([f"<span class=code>{pid}</span>", f"<span class=code>{e(mac)}</span>", e(nome or ""), f"<b>{reais(valor)}</b>", badge(rot, k),
                     "<div class=row>" + F(ses, "/admin/pagamentos/aprovar", hid, "Aprovar", "sm ok") + F(ses, "/admin/pagamentos/rejeitar", hid, "Rejeitar", "sm dan") + "</div>"])
    hist = []
    for t, m, mac, n, v, s in q("SELECT criado,metodo,mac,plano_nome,valor,status FROM pagamentos ORDER BY criado DESC LIMIT 30"):
        rot, k = STATUS.get(s, (s, "mu"))
        hist.append([hdr_fmt(t), e(METODOS_NOME.get(m, m or "")), f"<span class=code>{e(mac or '')}</span>", e(n or ""), reais(v), badge(rot, k)])
    return ("Pagamentos", "Formas de pagamento, confirmações e histórico",
            "<div class=card><h2>Aguardando confirmação</h2><p class=sub>Pagamentos por Pix manual que você precisa conferir no seu banco</p>"
            + tabela(["Ref.", "Dispositivo", "Plano", "Valor", "Status", ""], pend, "Nada pendente.") + "</div>"
            "<div class=card><h2>Métodos de pagamento</h2><p class=sub>Escolha o que seus clientes podem usar</p>"
            + F(ses, "/admin/pagamentos/metodos", metodos, "Salvar métodos", "pri", bloco=True) + "</div>"
            "<div class=card><h2>Histórico</h2><p class=sub>Últimos 30 registros</p>"
            + tabela(["Data", "Método", "Dispositivo", "Plano", "Valor", "Status"], hist, "Sem registros.") + "</div>")

def pg_vouchers(ses, local):
    livres = q("SELECT codigo,minutos FROM vouchers WHERE usado_por IS NULL ORDER BY criado DESC LIMIT 100")
    usados = [[f"<span class=code>{e(c)}</span>", fmt_dur(m), f"<span class=code>{e(u)}</span>", hdr_fmt(t)]
              for c, m, u, t in q("SELECT codigo,minutos,usado_por,usado_em FROM vouchers WHERE usado_por IS NOT NULL ORDER BY usado_em DESC LIMIT 20")]
    lista = "\n".join(f"{c}  ({fmt_dur(m)})" for c, m in livres)
    copiar = (f"<textarea id=vl readonly style='display:none'>{e(lista)}</textarea>"
              "<button class='btn sm' type=button onclick=\"var t=document.getElementById('vl');t.style.display='block';t.select();document.execCommand('copy');this.textContent='Copiado ✓'\">Copiar lista</button>") if livres else ""
    return ("Vouchers", "Códigos pré-pagos de acesso",
            "<div class=card><h2>Gerar vouchers</h2><p class=sub>Cada código vale uma vez, para um dispositivo.</p>"
            + F(ses, "/admin/vouchers/gerar", "<div class=cols><label>Quantidade<input name=qtd value=5></label><label>Minutos de cada um<input name=minutos value=60></label></div>", "Gerar vouchers", "pri", bloco=True) + "</div>"
            f"<div class=card><div class=row style='justify-content:space-between'><div><h2>Disponíveis</h2><p class=sub>{len(livres)} voucher(s) ainda não usados</p></div>{copiar}</div>"
            + tabela(["Código", "Tempo"], [[f"<span class=code>{e(c)}</span>", fmt_dur(m)] for c, m in livres], "Nenhum voucher disponível.") + "</div>"
            "<div class=card><h2>Usados recentemente</h2><p class=sub></p>" + tabela(["Código", "Tempo", "Dispositivo", "Quando"], usados, "Nenhum voucher usado ainda.") + "</div>")

def pg_bloqueios(ses, local):
    linhas = [[f"<span class=code>{e(m)}</span>", e(mo or "—"), hdr_fmt(t), F(ses, "/admin/bloqueios/rm", f"<input type=hidden name=mac value='{e(m)}'>", "Desbloquear", "sm")]
              for m, mo, t in q("SELECT mac,motivo,criado FROM bloqueados ORDER BY criado DESC")]
    return ("Bloqueios", "Dispositivos impedidos de comprar ou usar acesso",
            "<div class=card><h2>Bloquear dispositivo</h2><p class=sub>O bloqueio derruba a sessão atual e impede novas compras.</p>"
            + F(ses, "/admin/bloqueios/add", "<div class=cols><label>MAC<input name=mac placeholder='aa:bb:cc:dd:ee:ff'></label><label>Motivo<input name=motivo placeholder='Opcional'></label></div>", "Bloquear", "dan", bloco=True) + "</div>"
            "<div class=card><h2>Dispositivos bloqueados</h2><p class=sub></p>" + tabela(["Dispositivo", "Motivo", "Desde", ""], linhas, "Nenhum dispositivo bloqueado.") + "</div>")

def pg_sistema(ses, local):
    nova = atualizacao_disponivel()
    ult = int(cfg("upd_ultima") or 0)
    erro = cfg("upd_erro")
    if erro:
        estado = badge("Falha na verificação", "ba")
    elif nova:
        estado = badge(f"Nova versão {nova} disponível", "in")
    elif cfg("upd_versao_remota"):
        estado = badge("Atualizado", "ok")
    else:
        estado = badge("Ainda não verificado", "mu")
    acoes = F(ses, "/admin/sistema/verificar", "", "Verificar agora")
    if nova:
        acoes += F(ses, "/admin/sistema/instalar", "", f"Instalar v{e(nova)}", "pri", "Instalar a atualização? O portal reinicia em alguns segundos e as sessoes pagas sao mantidas.")
    if backups():
        acoes += F(ses, "/admin/sistema/reverter", "", "Voltar à versão anterior", "dan", "Voltar para a versao anterior? O portal reinicia.")
    url_campo = (f"<label>Link da atualização<input name=url value='{e(cfg('url_atualizacao'))}'></label>" if local else
                 f"<label>Link da atualização<input value='{e(cfg('url_atualizacao'))}' readonly><small>Por segurança, só pode ser alterado pelo próprio aparelho (ou pelo terminal: url-atualizacao).</small></label>")
    upd = ("<div class=card><h2>Atualização do sistema</h2><p class=sub>Com a opção automática ativa, o portal verifica o script do GitHub a cada 15 minutos. Se o código mudar, valida o arquivo, guarda um backup e reinicia para aplicar.</p>"
           f"<dl class=kv><dt>Versão instalada</dt><dd><span class=code>v{VERSION}</span></dd><dt>Última verificação</dt><dd>{hdr_fmt(ult) if ult else 'nunca'}</dd>"
           f"<dt>Situação</dt><dd>{estado}</dd>" + (f"<dt>Detalhe</dt><dd style='color:var(--ba)'>{e(erro)}</dd>" if erro else "") + "</dl>"
           f"<div class=row>{acoes}</div><hr style='border:0;border-top:1px solid var(--bd);margin:18px 0'>"
           + F(ses, "/admin/sistema/upd_cfg", url_campo + f"<label class=cb>{ck('upd_auto', cfg('upd_auto') == '1')} Instalar automaticamente quando o código mudar (verificação a cada 15 minutos; o portal reinicia)</label>", "Salvar", "", bloco=True) + "</div>")
    geral = ("<div class=card><h2>Geral</h2><p class=sub></p>"
             + F(ses, "/admin/sistema/salvar", f"<label>Nome da rede<input name=nome_rede value='{e(cfg('nome_rede'))}'></label>"
                 f"<label>WhatsApp do administrador<input name=whatsapp_admin value='{e(cfg('whatsapp_admin'))}' placeholder='+55 11 99999-9999' inputmode=tel></label><small>Informe o número em formato internacional; será usado no botão de contato dos clientes.</small>"
                 f"<label class=cb>{ck('admin_somente_local', cfg('admin_somente_local') == '1')} Painel admin só neste aparelho (mais seguro)</label>", "Salvar", "pri", bloco=True) + "</div>")
    av = alerta("Você ainda usa a senha padrão. Troque agora.", "wa") if cfg("senha_padrao") == "1" else ""
    senha = (f"<div class=card><h2>Senha do administrador</h2><p class=sub>Vale para o painel web e para o terminal.</p>{av}"
             + F(ses, "/admin/sistema/senha", "<label>Senha atual<input type=password name=atual autocomplete=current-password></label><div class=cols>"
                 "<label>Nova senha<input type=password name=nova autocomplete=new-password></label><label>Repita a nova senha<input type=password name=nova2 autocomplete=new-password></label></div>",
                 "Alterar senha", "pri", bloco=True) + "</div>")
    return "Sistema", "Atualizações, segurança e configurações gerais", upd + "<div class='grid g2'>" + geral + senha + "</div>"

PAGINAS = {"/admin": ("painel", pg_painel), "/admin/clientes": ("clientes", pg_clientes), "/admin/planos": ("planos", pg_planos),
           "/admin/pagamentos": ("pagamentos", pg_pagamentos), "/admin/vouchers": ("vouchers", pg_vouchers),
           "/admin/bloqueios": ("bloqueios", pg_bloqueios), "/admin/sistema": ("sistema", pg_sistema)}

# --- ações web (retornam (destino, mensagem))
def _int(v, padrao=None):
    try:
        return int(str(v).strip())
    except Exception:
        return padrao

def _preco(v):
    try:
        p = float(str(v).replace(",", ".").strip())
        return p if p >= 0 else None
    except Exception:
        return None

def a_ajustar(f):
    mac, mins = mac_ok(f.get("mac")), _int(f.get("min", f.get("minutos")))
    if not mac or mins is None:
        return "/admin/clientes", "MAC ou minutos inválidos."
    add_tempo(mac, mins)
    reconciliar()
    return "/admin/clientes", f"Tempo ajustado em {mins} min para {mac}."

def a_encerrar(f):
    mac = mac_ok(f.get("mac"))
    if mac:
        x("DELETE FROM sessoes WHERE mac=?", mac)
        reconciliar()
    return "/admin/clientes", "Sessão encerrada."

def a_fixo_add(f):
    mac = mac_ok(f.get("mac"))
    if not mac:
        return "/admin/clientes", "MAC inválido."
    x("REPLACE INTO fixos VALUES(?,?)", mac, f.get("nome", "")[:40])
    reconciliar()
    return "/admin/clientes", "Acesso permanente adicionado."

def a_fixo_rm(f):
    x("DELETE FROM fixos WHERE mac=?", f.get("mac", ""))
    reconciliar()
    return "/admin/clientes", "Removido."

def a_plano_novo(f):
    m, p = _int(f.get("minutos")), _preco(f.get("preco"))
    if not f.get("nome", "").strip() or not m or m < 1 or p is None:
        return "/admin/planos", "Dados do plano inválidos."
    x("INSERT INTO planos(nome,minutos,preco) VALUES(?,?,?)", f["nome"].strip()[:40], m, p)
    return "/admin/planos", "Plano criado."

def a_plano_salvar(f):
    m, p, i = _int(f.get("minutos")), _preco(f.get("preco")), _int(f.get("id"))
    if not f.get("nome", "").strip() or not m or m < 1 or p is None or i is None:
        return "/admin/planos", "Dados do plano inválidos."
    x("UPDATE planos SET nome=?,minutos=?,preco=?,ativo=? WHERE id=?", f["nome"].strip()[:40], m, p, 1 if f.get("ativo") == "1" else 0, i)
    return "/admin/planos", "Plano salvo."

def a_plano_excluir(f):
    x("DELETE FROM planos WHERE id=?", _int(f.get("id"), 0))
    return "/admin/planos", "Plano excluído."

def a_metodos(f):
    token_novo = f.get("mp_token", "").strip()
    if token_novo:
        cfg_set("mp_token", token_novo)
    mp_quer_ligar = f.get("metodo_mp") == "1"
    if mp_quer_ligar and not cfg("mp_token"):
        cfg_set("metodo_mp", "0")
        for k in ("metodo_pix_manual", "metodo_voucher"):
            cfg_set(k, "1" if f.get(k) == "1" else "0")
        cfg_set("pix_chave", f.get("pix_chave", "").strip()[:100])
        cfg_set("pix_nome", f.get("pix_nome", "").strip()[:60])
        return "/admin/pagamentos", "Não foi possível ativar Mercado Pago: configure o token primeiro."
    for k in ("metodo_mp", "metodo_pix_manual", "metodo_voucher"):
        cfg_set(k, "1" if f.get(k) == "1" else "0")
    cfg_set("pix_chave", f.get("pix_chave", "").strip()[:100])
    cfg_set("pix_nome", f.get("pix_nome", "").strip()[:60])
    return "/admin/pagamentos", "Métodos salvos. Mercado Pago " + ("ativado." if mp_quer_ligar else "desativado; clientes recebem 5 minutos iniciais e podem contatar o administrador.")

def a_aprovar(f):
    ok = aprovar_pagamento(f.get("id", ""))
    reconciliar()
    return "/admin/pagamentos", "Pagamento aprovado e acesso liberado." if ok else "Pagamento não encontrado."

def a_rejeitar(f):
    x("UPDATE pagamentos SET status='rejeitado' WHERE id=? AND status!='paid'", f.get("id", ""))
    return "/admin/pagamentos", "Pagamento rejeitado."

def a_vouchers(f):
    qtd, m = _int(f.get("qtd")), _int(f.get("minutos"))
    if not qtd or not m or not 1 <= qtd <= 200 or m < 1:
        return "/admin/vouchers", "Quantidade (1-200) ou minutos inválidos."
    gerar_vouchers(qtd, m)
    return "/admin/vouchers", f"{qtd} vouchers gerados."

def a_bloq_add(f):
    mac = mac_ok(f.get("mac"))
    if not mac:
        return "/admin/bloqueios", "MAC inválido."
    x("REPLACE INTO bloqueados VALUES(?,?,?)", mac, f.get("motivo", "")[:60], int(time.time()))
    x("DELETE FROM sessoes WHERE mac=?", mac)
    reconciliar()
    return "/admin/bloqueios", f"{mac} bloqueado."

def a_bloq_rm(f):
    x("DELETE FROM bloqueados WHERE mac=?", f.get("mac", ""))
    reconciliar()
    return "/admin/bloqueios", "Desbloqueado."

def a_config(f):
    numero_raw = f.get("whatsapp_admin", "").strip()
    numero = normalizar_whatsapp(numero_raw) if numero_raw else ""
    if numero_raw and not numero:
        return "/admin/sistema", "Número de WhatsApp inválido. Use formato internacional, com 8 a 15 dígitos."
    cfg_set("nome_rede", f.get("nome_rede", "").strip()[:40] or "Wi-Fi Livre")
    cfg_set("whatsapp_admin", numero)
    cfg_set("admin_somente_local", "1" if f.get("admin_somente_local") == "1" else "0")
    return "/admin/sistema", "Configurações salvas."

def a_senha(f):
    if not confere_senha(f.get("atual", "")):
        return "/admin/sistema", "Senha atual incorreta."
    if f.get("nova") != f.get("nova2"):
        return "/admin/sistema", "As senhas novas não conferem."
    erro = validar_senha_nova(f.get("nova", ""))
    if erro:
        return "/admin/sistema", erro
    definir_senha(f["nova"])
    return "/admin/sistema", "Senha alterada com sucesso."

def a_verificar(f):
    try:
        nova = verificar_atualizacao()
    except Exception as ex:
        return "/admin/sistema", "Falha ao verificar: " + str(ex)[:120]
    return "/admin/sistema", (f"Nova versão {nova} disponível!" if vtuple(nova) > vtuple(VERSION) else f"Você já está na versão mais recente ({VERSION}).")

def a_instalar(f):
    ok, msg = atualizar()
    if not ok:
        return "/admin/sistema", msg
    reiniciar_em()
    return "/admin/reiniciando", msg

def a_reverter(f):
    ok, msg = reverter()
    if not ok:
        return "/admin/sistema", msg
    reiniciar_em()
    return "/admin/reiniciando", msg

def a_upd_cfg(f):
    cfg_set("upd_auto", "1" if f.get("upd_auto") == "1" else "0")
    if f.get("_local") == "1" and f.get("url") is not None:
        try:
            url_download(f["url"])
        except ValueError as ex:
            return "/admin/sistema", str(ex)
        cfg_set("url_atualizacao", f["url"].strip())
    return "/admin/sistema", "Configurações de atualização salvas."

ACOES = {"/admin/clientes/ajustar": a_ajustar, "/admin/clientes/encerrar": a_encerrar,
         "/admin/clientes/fixo_add": a_fixo_add, "/admin/clientes/fixo_rm": a_fixo_rm,
         "/admin/planos/novo": a_plano_novo, "/admin/planos/salvar": a_plano_salvar,
         "/admin/planos/excluir": a_plano_excluir, "/admin/pagamentos/metodos": a_metodos,
         "/admin/pagamentos/aprovar": a_aprovar, "/admin/pagamentos/rejeitar": a_rejeitar,
         "/admin/vouchers/gerar": a_vouchers, "/admin/bloqueios/add": a_bloq_add,
         "/admin/bloqueios/rm": a_bloq_rm, "/admin/sistema/salvar": a_config,
         "/admin/sistema/senha": a_senha, "/admin/sistema/verificar": a_verificar,
         "/admin/sistema/instalar": a_instalar, "/admin/sistema/reverter": a_reverter,
         "/admin/sistema/upd_cfg": a_upd_cfg}

# ================================================================== terminal
def c_status(a):
    agora = int(time.time())
    n = q("SELECT COUNT(*) FROM sessoes WHERE expira>?", agora)[0][0]
    pend = q("SELECT COUNT(*) FROM pagamentos WHERE metodo='pix' AND status IN ('pending','analise')")[0][0]
    linhas = [f"Portal Wi-Fi v{VERSION} | Rede: {cfg('nome_rede')}", f"Clientes conectados: {n}",
              f"Pix manual pendentes: {pend}", f"Receita hoje: {reais(receita(inicio_do_dia()))}"]
    if atualizacao_disponivel():
        linhas.append(f"⬆ Nova versão {atualizacao_disponivel()} disponível! Use: atualizar")
    if cfg("senha_padrao") == "1":
        linhas.append("⚠ Senha admin ainda é a padrão! Use: senha")
    linhas.append("Acesso inicial: 5 minutos, uma vez por dispositivo; mais tempo é liberado pelo painel admin.")
    linhas.append("Mercado Pago: " + ("ativado" if cfg("metodo_mp") == "1" and cfg("mp_token") else "desativado"))
    return "\n".join(linhas)

def c_clientes(a):
    agora = time.time()
    r = q("SELECT mac,ip,expira FROM sessoes WHERE expira>? ORDER BY expira DESC", int(agora))
    return "\n".join(f"{m}  {ip or '-':15}  restam {fmt_tempo(ex - agora)}" for m, ip, ex in r) or "Nenhum cliente conectado."

def c_tempo(a):
    if len(a) < 2 or not mac_ok(a[0]) or _int(a[1]) is None:
        return "Uso: tempo MAC MINUTOS   (negativo remove tempo)"
    add_tempo(mac_ok(a[0]), _int(a[1])); reconciliar()
    return f"Ok: {_int(a[1])} min aplicados a {mac_ok(a[0])}."

def c_encerrar(a):
    if not a or not mac_ok(a[0]):
        return "Uso: encerrar MAC"
    x("DELETE FROM sessoes WHERE mac=?", mac_ok(a[0])); reconciliar()
    return "Sessão encerrada."

def c_bloquear(a):
    if not a or not mac_ok(a[0]):
        return "Uso: bloquear MAC [motivo]"
    m = mac_ok(a[0])
    x("REPLACE INTO bloqueados VALUES(?,?,?)", m, " ".join(a[1:])[:60], int(time.time()))
    x("DELETE FROM sessoes WHERE mac=?", m); reconciliar()
    return f"{m} bloqueado."

def c_desbloquear(a):
    if not a or not mac_ok(a[0]):
        return "Uso: desbloquear MAC"
    x("DELETE FROM bloqueados WHERE mac=?", mac_ok(a[0])); reconciliar()
    return "Desbloqueado."

def c_bloqueados(a):
    return "\n".join(f"{m}  {mo or ''}" for m, mo in q("SELECT mac,motivo FROM bloqueados")) or "Nenhum bloqueado."

def c_fixo(a):
    if not a or not mac_ok(a[0]):
        return "Uso: fixo MAC [nome]   |   fixos   |   fixo-rm MAC"
    x("REPLACE INTO fixos VALUES(?,?)", mac_ok(a[0]), " ".join(a[1:])[:40]); reconciliar()
    return "Acesso permanente concedido."

def c_fixo_rm(a):
    if not a or not mac_ok(a[0]):
        return "Uso: fixo-rm MAC"
    x("DELETE FROM fixos WHERE mac=?", mac_ok(a[0])); reconciliar()
    return "Removido."

def c_fixos(a):
    return "\n".join(f"{m}  {n or ''}" for m, n in q("SELECT mac,nome FROM fixos")) or "Nenhum acesso permanente."

def c_planos(a):
    return "\n".join(f"[{i}] {n} | {fmt_dur(m)} | {reais(p)} | {'ativo' if at else 'inativo'}"
                     for i, n, m, p, at in q("SELECT id,nome,minutos,preco,ativo FROM planos ORDER BY minutos")) or "Sem planos."

def c_plano_add(a):
    if len(a) < 3 or _int(a[1]) is None or _preco(a[2]) is None:
        return "Uso: plano-add NOME MINUTOS PRECO   (ex: plano-add Diario 1440 5.00)"
    x("INSERT INTO planos(nome,minutos,preco) VALUES(?,?,?)", a[0][:40], _int(a[1]), _preco(a[2]))
    return "Plano criado."

def c_plano_edit(a):
    if len(a) < 3 or _int(a[0]) is None or _int(a[1]) is None or _preco(a[2]) is None:
        return "Uso: plano-edit ID MINUTOS PRECO"
    x("UPDATE planos SET minutos=?,preco=? WHERE id=?", _int(a[1]), _preco(a[2]), _int(a[0]))
    return "Plano atualizado."

def c_plano_del(a):
    if not a or _int(a[0]) is None:
        return "Uso: plano-del ID"
    x("DELETE FROM planos WHERE id=?", _int(a[0])); return "Plano excluído."

def _plano_ativo(a, v):
    if not a or _int(a[0]) is None:
        return "Uso: plano-on ID | plano-off ID"
    x("UPDATE planos SET ativo=? WHERE id=?", v, _int(a[0])); return "Ok."

def c_plano_on(a): return _plano_ativo(a, 1)
def c_plano_off(a): return _plano_ativo(a, 0)

CHAVES_METODO = {"mp": "metodo_mp", "pix": "metodo_pix_manual", "voucher": "metodo_voucher"}

def c_metodos(a):
    ln = [f"{n:8} {'LIGADO' if cfg(k) == '1' else 'desligado'}" for n, k in CHAVES_METODO.items()]
    ln.append(f"Token MP: {'configurado' if cfg('mp_token') else 'não configurado'} | Chave Pix: {cfg('pix_chave') or '-'} ({cfg('pix_nome') or '-'})")
    return "\n".join(ln)

def c_metodo(a):
    if len(a) < 2 or a[0] not in CHAVES_METODO or a[1] not in ("on", "off"):
        return "Uso: metodo mp|pix|voucher on|off"
    if a[0] == "mp" and a[1] == "on" and not cfg("mp_token"):
        return "Configure o token primeiro com: mp-token"
    cfg_set(CHAVES_METODO[a[0]], "1" if a[1] == "on" else "0")
    return c_metodos([])

def c_mp_token(a):
    t = a[0] if a else getpass.getpass("Token Mercado Pago (não aparece): ")
    if t.strip():
        cfg_set("mp_token", t.strip())
        return "Token salvo. O Mercado Pago permanece no estado atual; use 'metodo mp on|off' para controlar a ativação."
    return "Cancelado."

def c_whatsapp(a):
    if not a:
        return "WhatsApp do administrador: " + (cfg("whatsapp_admin") or "não configurado") + "\nUso: whatsapp +5511999999999"
    numero = normalizar_whatsapp(" ".join(a))
    if not numero:
        return "Número inválido. Use formato internacional com 8 a 15 dígitos, ex.: whatsapp +5511999999999"
    cfg_set("whatsapp_admin", numero)
    return "WhatsApp do administrador salvo."

def c_pix_chave(a):
    if not a:
        return "Uso: pix-chave CHAVE [nome do titular]"
    cfg_set("pix_chave", a[0]); cfg_set("pix_nome", " ".join(a[1:])); cfg_set("metodo_pix_manual", "1")
    return "Chave salva e Pix manual ligado."

def c_voucher(a):
    if len(a) < 2 or _int(a[0]) is None or _int(a[1]) is None or not 1 <= _int(a[0]) <= 200 or _int(a[1]) < 1:
        return "Uso: voucher QUANTIDADE MINUTOS   (ex: voucher 10 60)"
    return "\n".join(gerar_vouchers(_int(a[0]), _int(a[1])))

def c_vouchers(a):
    return "\n".join(f"{c}  {fmt_dur(m)}" for c, m in q("SELECT codigo,minutos FROM vouchers WHERE usado_por IS NULL ORDER BY criado DESC")) or "Nenhum voucher disponível."

def c_pendentes(a):
    return "\n".join(f"{i} | {mac} | {n} | {reais(v)} | {s}" for i, mac, n, v, s in
                     q("SELECT id,mac,plano_nome,valor,status FROM pagamentos WHERE metodo='pix' AND status IN ('pending','analise') ORDER BY criado DESC")) or "Nenhum pendente."

def c_aprovar(a):
    if not a:
        return "Uso: aprovar ID"
    ok = aprovar_pagamento(a[0]); reconciliar()
    return "Aprovado e liberado." if ok else "Pagamento não encontrado/já pago."

def c_rejeitar(a):
    if not a:
        return "Uso: rejeitar ID"
    x("UPDATE pagamentos SET status='rejeitado' WHERE id=? AND status!='paid'", a[0]); return "Rejeitado."

def c_historico(a):
    return "\n".join(f"{hdr_fmt(t)} | {m} | {mac} | {n} | {reais(v)} | {s}" for t, m, mac, n, v, s in
                     q("SELECT criado,metodo,mac,plano_nome,valor,status FROM pagamentos ORDER BY criado DESC LIMIT 30")) or "Sem pagamentos."

def c_config(a):
    if len(a) < 2 or a[0] not in ("nome_rede", "admin_somente_local", "upd_auto"):
        return "Uso: config nome_rede NOME... | config admin_somente_local 0|1 | config upd_auto 0|1"
    cfg_set(a[0], " ".join(a[1:])); return "Salvo."

def c_senha(a):
    s1 = getpass.getpass("Nova senha admin: ")
    erro = validar_senha_nova(s1)
    if erro:
        return erro
    if s1 != getpass.getpass("Repita a nova senha: "):
        return "As senhas não conferem."
    definir_senha(s1)
    return "Senha alterada (vale para o terminal e para o painel web)."

def c_senha_padrao(a):
    definir_senha(SENHA_PADRAO)
    return f"Senha redefinida para a padrão ({SENHA_PADRAO}). Altere com: senha"

def c_versao(a):
    return (f"Versão instalada: {VERSION}\nVersão disponível: {cfg('upd_versao_remota') or '(não verificado)'}\n"
            f"Link: {cfg('url_atualizacao')}\nBackups: {len(backups())}")

def c_verificar(a):
    try:
        nova = verificar_atualizacao()
    except Exception as ex:
        return "Falha ao verificar: " + str(ex)
    if vtuple(nova) > vtuple(VERSION):
        return f"Nova versão disponível: {nova} (instalada: {VERSION}). Use: atualizar"
    return f"Você está na versão mais recente ({VERSION})."

def c_atualizar(a):
    global REINICIAR_PENDENTE
    ok, msg = atualizar(forcar="forcar" in a)
    if ok:
        REINICIAR_PENDENTE = True
    return msg

def c_reverter(a):
    global REINICIAR_PENDENTE
    ok, msg = reverter()
    if ok:
        REINICIAR_PENDENTE = True
    return msg

def c_url_atualizacao(a):
    if not a:
        return "Link atual: " + cfg("url_atualizacao") + "\nPara trocar: url-atualizacao https://..."
    try:
        url_download(a[0])
    except ValueError as ex:
        return str(ex)
    cfg_set("url_atualizacao", a[0]); cfg_set("upd_versao_remota", "")
    return "Link de atualização salvo."

COMANDOS = {
    "status": (c_status, "resumo geral"), "clientes": (c_clientes, "clientes conectados"),
    "tempo": (c_tempo, "MAC MIN  — soma/remove minutos"), "encerrar": (c_encerrar, "MAC — derruba o cliente"),
    "bloquear": (c_bloquear, "MAC [motivo]"), "desbloquear": (c_desbloquear, "MAC"), "bloqueados": (c_bloqueados, "lista"),
    "fixo": (c_fixo, "MAC [nome] — acesso permanente"), "fixo-rm": (c_fixo_rm, "MAC"), "fixos": (c_fixos, "lista"),
    "planos": (c_planos, "lista de planos"), "plano-add": (c_plano_add, "NOME MIN PRECO"),
    "plano-edit": (c_plano_edit, "ID MIN PRECO"), "plano-del": (c_plano_del, "ID"),
    "plano-on": (c_plano_on, "ID"), "plano-off": (c_plano_off, "ID"),
    "metodos": (c_metodos, "ver métodos de pagamento"), "metodo": (c_metodo, "mp|pix|voucher|teste on|off"),
    "mp-token": (c_mp_token, "[TOKEN] — salva o token sem ativar o Mercado Pago"), "whatsapp": (c_whatsapp, "[NÚMERO] — configura o WhatsApp do administrador"), "pix-chave": (c_pix_chave, "CHAVE [titular]"),
    "voucher": (c_voucher, "QTD MIN — gera vouchers"), "vouchers": (c_vouchers, "vouchers disponíveis"),
    "pendentes": (c_pendentes, "Pix manual aguardando"), "aprovar": (c_aprovar, "ID"), "rejeitar": (c_rejeitar, "ID"),
    "historico": (c_historico, "últimos pagamentos"), "config": (c_config, "nome_rede|admin_somente_local|upd_auto VALOR"),
    "senha": (c_senha, "altera a senha do admin"), "senha-padrao": (c_senha_padrao, "volta para a senha padrão"),
    "versao": (c_versao, "versão instalada e disponível"), "verificar": (c_verificar, "procura atualização"),
    "atualizar": (c_atualizar, "[forcar] — baixa e instala a nova versão"), "reverter": (c_reverter, "volta à versão anterior"),
    "url-atualizacao": (c_url_atualizacao, "[URL] — vê/troca o link de atualização"),
}

def executar(tokens):
    if not tokens or tokens[0] in ("ajuda", "help", "-h", "--help"):
        return "Comandos:\n" + "\n".join(f"  {k:15} {d}" for k, (_, d) in COMANDOS.items())
    c = COMANDOS.get(tokens[0])
    return c[0](tokens[1:]) if c else "Comando desconhecido. Digite: ajuda"

MENU = [("Status geral", "status", []), ("Clientes conectados", "clientes", []),
        ("Dar/remover tempo de um cliente", "tempo", ["MAC", "Minutos (negativo remove)"]),
        ("Encerrar cliente", "encerrar", ["MAC"]), ("Bloquear dispositivo", "bloquear", ["MAC"]),
        ("Desbloquear dispositivo", "desbloquear", ["MAC"]), ("Ver planos", "planos", []),
        ("Criar plano", "plano-add", ["Nome (sem espaços)", "Minutos", "Preço"]),
        ("Editar plano", "plano-edit", ["ID", "Minutos", "Preço"]), ("Excluir plano", "plano-del", ["ID"]),
        ("Métodos de pagamento", "metodos", []), ("Ligar/desligar método", "metodo", ["Método (mp/pix/voucher)", "on ou off"]),
        ("Definir token Mercado Pago", "mp-token", []), ("Definir WhatsApp do administrador", "whatsapp", ["Número internacional ou vazio para consultar"]), ("Definir chave Pix manual", "pix-chave", ["Chave Pix", "Nome do titular"]),
        ("Pix pendentes", "pendentes", []), ("Aprovar pagamento", "aprovar", ["ID"]),
        ("Gerar vouchers", "voucher", ["Quantidade", "Minutos"]), ("Alterar senha admin", "senha", []),
        ("Histórico de pagamentos", "historico", []),
        ("Verificar atualização", "verificar", []), ("Instalar atualização", "atualizar", []),
        ("Voltar à versão anterior", "reverter", [])]

def menu_terminal():
    global REINICIAR_PENDENTE
    print("\n=== ADMIN (digite o número, um comando, 'ajuda' ou 'sair') ===")
    while True:
        print()
        for i, (rot, _, _) in enumerate(MENU, 1):
            print(f" {i:2}) {rot}")
        try:
            op = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if op in ("sair", "exit", "0"):
            return
        if op.isdigit() and 1 <= int(op) <= len(MENU):
            _, cmd, perguntas = MENU[int(op) - 1]
            try:
                args = [input(f"  {p}: ").strip() for p in perguntas]
            except (EOFError, KeyboardInterrupt):
                continue
            print(executar([cmd] + args))
        elif op:
            try:
                print(executar(shlex.split(op)))
            except ValueError as ex:
                print("Erro:", ex)
        if REINICIAR_PENDENTE and SERVIDOR_AQUI:
            print("\nReiniciando o portal para aplicar a mudança…")
            reiniciar()

# ==================================================================== início
def ip_hotspot():
    r = sh(["ip", "-4", "addr", "show", IFACE])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
    return m.group(1) if m else "IP_DO_HOTSPOT"

def iniciar_servidor():
    global SERVIDOR_AQUI
    firewall_setup()
    atexit.register(firewall_cleanup)
    reconciliar()
    threading.Thread(target=loop_reconciliar, daemon=True).start()
    threading.Thread(target=loop_atualizacao, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORTA), H)
    except OSError as ex:
        sys.exit(f"Não foi possível abrir a porta {PORTA}: {ex}")
    SERVIDOR_AQUI = True
    print(f"Portal v{VERSION} ativo | interface {IFACE} | painel: http://{ip_hotspot()}:{PORTA}/admin  (neste aparelho: http://127.0.0.1:{PORTA}/admin)")
    if cfg("senha_padrao") == "1":
        print(f"⚠ Senha admin padrão em uso ({SENHA_PADRAO}) — altere com o comando: senha")
    print(f"Ideia e desenvolvimento: khallendev")
    return srv

if __name__ == "__main__":
    iniciar_db()
    args = sys.argv[1:]
    if not args:
        srv = iniciar_servidor()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(c_status([]))
        menu_terminal()
    elif args[0] == "servidor":
        srv = iniciar_servidor()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        print(executar(args))
        if REINICIAR_PENDENTE:
            print("Reinicie o portal para aplicar a nova versão (pare com Ctrl+C e rode de novo).")
        elif args[0] not in ("ajuda", "help", "versao", "verificar", "atualizar"):
            print("(se o portal estiver rodando, o firewall é atualizado em até 5 segundos)")
