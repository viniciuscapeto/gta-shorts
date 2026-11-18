"""
Painel da fábrica: http://localhost:8765
Tudo pelo navegador: buscar clipes, aprovar, editar textos, controlar as postagens
por rede, ver os números, mudar as configurações e acompanhar o registro.
Abre com  painel.bat  (ou  python fabrica.py --painel).
"""
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

F = None  # o módulo fabrica (recebido em iniciar)
WEB = Path(__file__).resolve().parent / "painel"
TIPOS = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon"}

# o que dá pra mudar pela aba Configurações (caminho dentro do config.json)
EDITAVEIS = [
    "nome_da_pagina", "seu_arroba", "narracao.ativar", "legenda.ativar",
    "twitch.descobrir.ativar", "twitch.descobrir.dias", "twitch.descobrir.min_visualizacoes",
    "twitch.descobrir.max_clipes", "twitch.descobrir.max_por_streamer",
    "twitch.dias", "twitch.clipes_por_streamer", "twitch.min_visualizacoes",
    "twitch.streamers", "twitch.nunca_pegar", "twitch.hashtags", "twitch.usar_titulo_do_clipe",
    "twitch.streamers_qualquer_jogo", "twitch.so_autorizados",
    "youtube.buscas", "youtube.canais", "youtube.nunca_pegar", "youtube.dias",
    "youtube.min_visualizacoes", "youtube.min_duracao_min", "youtube.max_videos",
    "youtube.cortes_por_video", "youtube.segundos_por_corte",
]


# ---------------------------------------------------------------- registro (log)
class Registro:
    """Pega tudo o que a fábrica imprime e guarda as últimas linhas pro painel."""

    def __init__(self):
        self.linhas, self.atual, self.trava = [], "", threading.Lock()

    def write(self, s):
        try:
            sys.__stdout__.write(s)
        except Exception:
            pass
        with self.trava:
            self.atual += s
            *prontas, self.atual = self.atual.split("\n")
            self.linhas += [(time.time(), l.rstrip()) for l in prontas if l.strip()]
            self.linhas = self.linhas[-600:]

    def flush(self):
        try:
            sys.__stdout__.flush()
        except Exception:
            pass

    def ultimas(self, n=300):
        with self.trava:
            extra = [(time.time(), self.atual.rstrip())] if self.atual.strip() else []
            return [{"t": t, "txt": l} for t, l in (self.linhas + extra)[-n:]]


REG = Registro()
TAREFA = {"nome": None, "inicio": None, "ultima": None}
TELEGRAM = {"thread": None}


TRAVA_TAREFA = threading.Lock()


def rodar_tarefa(nome: str, func):
    with TRAVA_TAREFA:  # dois cliques rápidos não podem disparar duas buscas
        if TAREFA["nome"]:
            raise RuntimeError(f"Espere terminar: {TAREFA['nome']}")
        TAREFA.update(nome=nome, inicio=time.time())

    def alvo():
        ok = True
        print(f"▶ {nome}")
        try:
            func()
        except Exception as e:  # noqa: BLE001 - mostra qualquer erro no registro
            ok = False
            print(f"❌ Erro: {e}")
        finally:
            TAREFA.update(nome=None, inicio=None, ultima={"nome": nome, "fim": time.time(), "ok": ok})
            print(f"■ {nome}: {'pronto' if ok else 'parou com erro'}")

    threading.Thread(target=alvo, daemon=True).start()


def cfg_com_legenda():
    cfg = F.carregar_config()
    if cfg.get("legenda", {}).get("ativar") and not F.legenda_disponivel() and not F.preparar_whisper(cfg):
        print("Seguindo sem legenda (não consegui baixar o whisper).")
        cfg["legenda"]["ativar"] = False
    return cfg


TAREFAS = {
    "buscar": ("Buscar clipes virais da Twitch", lambda: F.modo_twitch(cfg_com_legenda())),
    "listar": ("Ver quais clipes pegaria", lambda: F.modo_twitch(F.carregar_config(), so_listar=True)),
    "youtube": ("Buscar momentos virais do YouTube", lambda: F.modo_youtube(cfg_com_legenda())),
    "youtube_listar": ("Ver quais trechos do YouTube pegaria",
                       lambda: F.modo_youtube(F.carregar_config(), so_listar=True)),
    "gravacoes": ("Cortar minhas gravações (pasta entrada)", lambda: F.modo_auto(cfg_com_legenda())),
    "curiosidades": ("Gerar vídeos de curiosidade", lambda: F.modo_curiosidade(F.carregar_config())),
}


def ouvir_telegram():
    """Liga a escuta dos botões do Telegram em segundo plano (se o bot estiver configurado)."""
    t = TELEGRAM["thread"]
    if t and t.is_alive():
        return True
    if not str(F.ler_segredos().get("telegram_token", "")).strip():
        return False
    if F.carregar_config().get("instagram", {}).get("postar_pela_nuvem"):
        return False  # quem lê as aprovações agora é a nuvem (senão um "rouba" o clique do outro)
    TELEGRAM["thread"] = threading.Thread(target=F.modo_aprovar, daemon=True)
    TELEGRAM["thread"].start()
    return True


# ---------------------------------------------------------------- dados pro painel
def _pegar(d: dict, caminho: str):
    for k in caminho.split("."):
        d = d.get(k, {}) if isinstance(d, dict) else {}
    return None if d == {} else d


def _colocar(d: dict, caminho: str, valor):
    *pais, ult = caminho.split(".")
    for k in pais:
        d = d.setdefault(k, {})
    d[ult] = valor


def estado() -> dict:
    seg, cfg = F.ler_segredos(), F.carregar_config()
    streamers = cfg.get("twitch", {}).get("streamers", [])
    t = TELEGRAM["thread"]
    return {
        "nome": cfg.get("nome_da_pagina", ""), "arroba": cfg.get("seu_arroba", ""),
        "tarefa": TAREFA["nome"], "desde": TAREFA["inicio"], "ultima": TAREFA["ultima"],
        "twitch": bool(str(seg.get("twitch_client_id", "")).strip()
                       and str(seg.get("twitch_client_secret", "")).strip()),
        "telegram": bool(str(seg.get("telegram_token", "")).strip()),
        "telegram_chat": bool(str(seg.get("telegram_chat_id", "")).strip()),
        "telegram_ouvindo": bool(t and t.is_alive()),
        "autorizados": sum(1 for s in streamers if s.get("autorizado")),
        "descobrir": bool(cfg.get("twitch", {}).get("descobrir", {}).get("ativar", True)),
        "gravacoes": sum(1 for p in F.ENTRADA.glob("*") if p.suffix.lower() in F.EXTS)
                     if F.ENTRADA.exists() else 0,
        "logs": REG.ultimas(),
    }


def salvar_config(mudancas: dict):
    cfg = F.carregar_config()
    for caminho, valor in mudancas.items():
        if caminho in EDITAVEIS:
            _colocar(cfg, caminho, valor)
    with open(F.BASE / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("💾 Configurações salvas")


def salvar_segredos(dados: dict):
    seg = F.ler_segredos()
    for k in ("twitch_client_id", "twitch_client_secret", "telegram_token"):
        v = str(dados.get(k, "")).strip()
        if v:
            if k == "telegram_token" and v != seg.get(k):
                seg["telegram_chat_id"] = ""      # bot novo: descobre o chat de novo
                seg.pop("_telegram_offset", None)
            seg[k] = v
    F.salvar_segredos(seg)
    print("🔑 Chaves salvas")


def salvar_textos(nome: str, titulo: str, legenda: str) -> bool:
    for pasta in (F.SAIDA, F.APROVADOS, F.RECUSADOS):
        txt = pasta / (Path(nome).stem + ".txt")
        if (pasta / Path(nome).name).exists():
            antigo = txt.read_text(encoding="utf-8", errors="ignore") if txt.exists() else ""
            resto = re.search(r"\n\n(NARRAÇÃO|FALAS DO JOGO).*", antigo, re.S)
            txt.write_text(f"TÍTULO:\n{titulo.strip()}\n\nLEGENDA (copiar e colar):\n{legenda.strip()}"
                           f"{resto.group(0) if resto else ''}\n", encoding="utf-8")
            return True
    return False


def mudar_rede(nome: str, rede: str, st: str) -> bool:
    if rede not in F.REDES or st not in ("a_postar", "agendado", "postado", "pular"):
        return False
    est = json.loads(F.ESTADO_REDES.read_text(encoding="utf-8")) if F.ESTADO_REDES.exists() else {}
    v = est.setdefault(Path(nome).stem, {})
    v[rede] = st
    if st == "postado":
        v[f"_t_{rede}"] = time.time()  # pros gráficos
    else:
        v.pop(f"_t_{rede}", None)
    F.ESTADO_REDES.write_text(json.dumps(est, indent=1), encoding="utf-8")
    return True


def config_editavel() -> dict:
    cfg = F.carregar_config()
    return {c: _pegar(cfg, c) for c in EDITAVEIS}


# ---------------------------------------------------------------- servidor
class Painel(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _resp(self, corpo: bytes, tipo: str, codigo=200, extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados, codigo=200):
        self._resp(json.dumps(dados, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", codigo)

    def do_GET(self):
        caminho = urllib.parse.unquote(self.path.split("?")[0])
        if caminho == "/api/estado":
            return self._json(estado())
        if caminho == "/api/videos":
            return self._json({"videos": F.listar_videos()})
        if caminho == "/api/config":
            return self._json(config_editavel())
        if caminho == "/logo.png":
            logo = (sorted((F.BASE / "perfil").glob("perfil_tresestrelas*.png"))
                    or sorted((F.BASE / "perfil").glob("*.png")))
            if logo:
                return self._resp(logo[0].read_bytes(), "image/png")
            return self._json({"erro": "sem logo"}, 404)
        if caminho.startswith("/video/"):
            _, _, status, nome = caminho.split("/", 3)
            pasta = {"pendente": F.SAIDA, "aprovado": F.APROVADOS, "recusado": F.RECUSADOS}.get(status, F.SAIDA)
            arq = pasta / Path(nome).name
            if arq.suffix == ".mp4" and arq.exists():
                return self._video(arq)
            return self._json({"erro": "não achei"}, 404)
        arq = (WEB / ("index.html" if caminho == "/" else caminho.lstrip("/"))).resolve()
        if WEB in arq.parents and arq.is_file():
            return self._resp(arq.read_bytes(), TIPOS.get(arq.suffix, "application/octet-stream"))
        self._json({"erro": "não achei"}, 404)

    def _video(self, arq: Path):
        """Manda o vídeo aceitando Range (sem isso não dá pra avançar o vídeo)."""
        tam = arq.stat().st_size
        ini, fim = 0, tam - 1
        faixa = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if faixa and (faixa.group(1) or faixa.group(2)):
            if faixa.group(1):
                ini = int(faixa.group(1))
                fim = min(int(faixa.group(2)), tam - 1) if faixa.group(2) else tam - 1
            else:
                ini = max(0, tam - int(faixa.group(2)))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tam}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fim - ini + 1))
        self.end_headers()
        try:
            with open(arq, "rb") as f:
                f.seek(ini)
                falta = fim - ini + 1
                while falta > 0 and (bloco := f.read(min(1 << 20, falta))):
                    self.wfile.write(bloco)
                    falta -= len(bloco)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass  # o navegador cancelou (normal quando você pula o vídeo)

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            nome = Path(d.get("nome", "")).name
            rota = self.path
            if rota == "/api/tarefa":
                if d.get("tarefa") not in TAREFAS:
                    raise RuntimeError("tarefa desconhecida")
                rodar_tarefa(*TAREFAS[d["tarefa"]])
                return self._json({"ok": True})
            if rota == "/api/status":
                pasta = {"aprovado": F.APROVADOS, "recusado": F.RECUSADOS, "pendente": F.SAIDA}.get(d.get("status"))
                if pasta and d.get("titulo") is not None:
                    salvar_textos(nome, d["titulo"], d.get("legenda", ""))
                ok = bool(pasta and nome and F.mover_video(nome, pasta))
                if ok:
                    print(f"{'✅ Aprovado' if pasta == F.APROVADOS else '❌ Recusado' if pasta == F.RECUSADOS else '↩ Voltou pra fila'}: {nome}")
                return self._json({"ok": ok})
            if rota == "/api/textos":
                return self._json({"ok": salvar_textos(nome, d.get("titulo", ""), d.get("legenda", ""))})
            if rota == "/api/rede":
                return self._json({"ok": mudar_rede(nome, d.get("rede"), d.get("estado"))})
            if rota == "/api/config":
                salvar_config(d)
                return self._json({"ok": True})
            if rota == "/api/segredos":
                salvar_segredos(d)
                return self._json({"ok": True})
            if rota == "/api/testar/twitch":
                return self._json({"ok": bool(F.twitch_conectar())})
            if rota == "/api/testar/telegram":
                ok = bool(F.telegram_conectar())
                if ok:
                    ouvir_telegram()
                return self._json({"ok": ok})
            if rota == "/api/abrir-pasta":
                os.startfile(F.SAIDA)  # noqa: S606 - só abre a pasta no Explorer
                return self._json({"ok": True})
            self._json({"erro": "não achei"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"erro": str(e)}, 400)


def iniciar(fabrica, porta: int = 8765):
    global F
    F = fabrica
    sys.stdout = REG
    F.SAIDA.mkdir(exist_ok=True)
    url = f"http://localhost:{porta}"
    try:
        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", porta), Painel)
    except OSError:
        print(f"O painel já está aberto em {url}")
        webbrowser.open(url)
        return
    print(f"🚀 Painel no ar: {url}  (não feche esta janela enquanto estiver usando)")
    if ouvir_telegram():
        print("📲 Ouvindo as aprovações do Telegram")
    if "--sem-navegador" not in sys.argv:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        sys.stdout = sys.__stdout__
        print("\nPainel fechado.")
