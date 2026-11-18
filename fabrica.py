"""
Fabrica de Shorts - GTA 6
Pega gravações longas do PS5, acha os momentos mais agitados pelo áudio
(tiros, explosões, batidas, gritos), corta e exporta em vertical 9:16
com texto de gancho, pronto pra TikTok / Shorts / Kwai / Reels.

Uso:
    python fabrica.py              -> modo automático (pasta entrada/)
    python fabrica.py --listar     -> só mostra os momentos achados, não renderiza
    python fabrica.py --manual     -> corta exatamente o que estiver em cortes.csv
    python fabrica.py --sem-voz    -> (junto com os outros) desliga a narração
    python fabrica.py --testar-voz -> gera saida/teste_voz.wav pra ouvir a voz escolhida
    python fabrica.py --sem-legenda -> (junto com os outros) desliga a legenda das falas
    python fabrica.py --curiosidade -> narra as curiosidades do config.json por cima do gameplay
    python fabrica.py --instalar-legenda -> baixa o whisper.cpp + modelo (uma vez só)
    python fabrica.py --twitch      -> baixa os clipes de GTA mais vistos dos streamers
                                       autorizados (config.json > twitch) e edita com crédito
    python fabrica.py --twitch-listar -> só mostra quais clipes pegaria
    python fabrica.py --youtube     -> acha os vídeos longos de GTA mais vistos do YouTube,
                                       corta o trecho mais repetido e edita com crédito
    python fabrica.py --youtube-listar -> só mostra quais trechos pegaria
    python fabrica.py --painel      -> abre o painel no navegador (aprovar + controle por rede)
    python fabrica.py --testar-telegram -> conecta o bot do Telegram (1ª vez)
    python fabrica.py --aprovar     -> fica ouvindo os botões Aprovar/Recusar do Telegram
                                       e move os vídeos pra saida/aprovados ou recusados
"""
import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import imageio_ffmpeg

BASE = Path(__file__).resolve().parent
ENTRADA = BASE / "entrada"
SAIDA = BASE / "saida"
TMP = BASE / ".tmp"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
EXTS = {".mp4", ".webm", ".mov", ".mkv"}
# yt-dlp usando os certificados do Windows (os mesmos do Chrome): funciona também em
# rede que inspeciona HTTPS com certificado próprio
YTDLP = [sys.executable, "-m", "yt_dlp", "--compat-options", "no-certifi"]
SR = 8000          # taxa de amostragem usada só para análise
JANELA = 0.5       # segundos por bloco de análise


def carregar_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    # na nuvem (Linux) não existe C:/Windows/Fonts: o robô do GitHub informa outra fonte
    if os.environ.get("FABRICA_FONTE"):
        cfg["fonte"] = os.environ["FABRICA_FONTE"]
    return cfg


# ---------------------------------------------------------------- análise
def ler_audio(video: Path) -> np.ndarray:
    cmd = [FFMPEG, "-v", "error", "-i", str(video), "-vn", "-ac", "1",
           "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def achar_momentos(video: Path, cfg: dict):
    """Retorna (duracao, lista de (inicio, fim, pico, nota))."""
    audio = ler_audio(video)
    duracao = len(audio) / SR
    n = int(SR * JANELA)
    blocos = len(audio) // n
    if blocos < 10:
        return duracao, []
    rms = np.sqrt(np.mean(audio[: blocos * n].reshape(blocos, n) ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-6)

    # linha de base = mediana móvel de ~60s (o "volume normal" do trecho)
    meia = int(30 / JANELA)
    pad = np.pad(db, meia, mode="edge")
    base = np.median(np.lib.stride_tricks.sliding_window_view(pad, 2 * meia + 1), axis=1)
    excesso = np.clip(db - base, 0, None)

    # suaviza ~3s: valoriza ação sustentada, não só um estalo isolado
    k = int(3 / JANELA)
    nota = np.convolve(excesso, np.ones(k) / k, mode="same")

    antes, depois = cfg["segundos_antes_do_pico"], cfg["segundos_depois_do_pico"]
    momentos = []
    nota = nota.copy()
    while len(momentos) < cfg["max_cortes_por_video"]:
        i = int(np.argmax(nota))
        if nota[i] < cfg["sensibilidade_minima_db"]:
            break
        pico = i * JANELA
        ini, fim = max(0.0, pico - antes), min(duracao, pico + depois)
        if fim - ini >= 8:
            momentos.append((ini, fim, pico, float(nota[i])))
        # não deixa escolher outro corte sobreposto a este
        a = max(0, int((ini - depois) / JANELA))
        b = min(len(nota), int((fim + antes) / JANELA) + 1)
        nota[a:b] = 0
    momentos.sort(key=lambda m: m[0])
    return duracao, momentos


# ---------------------------------------------------------------- narração
def _edge_tts(texto: str, mp3: Path, narr: dict) -> list:
    """Gera o mp3 e devolve o tempo de cada palavra [(inicio, fim, palavra)]."""
    import edge_tts  # pip install edge-tts
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    com = edge_tts.Communicate(texto, narr.get("voz", "pt-BR-AntonioNeural"),
                               rate=narr.get("velocidade", "+0%"),
                               pitch=narr.get("tom", "+0Hz"), proxy=proxy,
                               boundary="WordBoundary")
    palavras = []

    async def rodar():
        with open(mp3, "wb") as f:
            async for c in com.stream():
                if c["type"] == "audio":
                    f.write(c["data"])
                elif c["type"] == "WordBoundary":
                    a = c["offset"] / 1e7
                    palavras.append((a, a + c["duration"] / 1e7, c["text"]))
    asyncio.run(rodar())
    return palavras


def _estimar_palavras(texto: str, dur: float) -> list:
    """Sem tempos da voz (voz do Windows): reparte a duração pelo tamanho das palavras."""
    ws = texto.split()
    total = sum(len(w) + 1 for w in ws) or 1
    t, out = 0.0, []
    for w in ws:
        d = dur * (len(w) + 1) / total
        out.append((t, t + d, w))
        t += d
    return out


def _voz_windows(texto: str, wav: Path):
    """Plano B offline: voz do próprio Windows (Microsoft Maria, pt-BR)."""
    txt = wav.with_suffix(".txt")
    txt.write_text(texto, encoding="utf-8")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try { $s.SelectVoiceByHints('NotSet','NotSet',0,[Globalization.CultureInfo]'pt-BR') } catch {};"
        "$s.Rate = 1;"
        f"$s.SetOutputToWaveFile('{wav}');"
        f"$s.Speak([IO.File]::ReadAllText('{txt}', [Text.Encoding]::UTF8));"
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True)
    txt.unlink(missing_ok=True)


def gerar_fala(texto: str, cfg: dict, com_palavras: bool = False):
    """Gera (ou reaproveita do cache) um .wav com a fala.
    Retorna (arquivo, duração) ou, com com_palavras=True, (arquivo, duração, palavras)."""
    narr = cfg.get("narracao", {})
    pasta = TMP / "falas"
    pasta.mkdir(parents=True, exist_ok=True)
    chave = f"{narr.get('voz')}|{narr.get('velocidade')}|{narr.get('tom')}|{texto}"
    wav = pasta / (hashlib.md5(chave.encode("utf-8")).hexdigest()[:16] + ".wav")
    tempos = wav.with_suffix(".json")
    palavras = None
    if wav.exists() and tempos.exists():
        palavras = json.loads(tempos.read_text(encoding="utf-8"))
    else:
        bruto, destino = wav.with_suffix(".mp3"), wav
        for tentativa in range(3):
            try:
                palavras = _edge_tts(texto, bruto, narr)
                break
            except Exception as e:
                erro = type(e).__name__
        else:
            print(f"\n   [voz online falhou ({erro}); usando voz do Windows]", end="")
            bruto = wav.with_suffix(".sapi.wav")
            destino = wav.with_name(wav.stem + "_windows.wav")  # não entra no cache
            _voz_windows(texto, bruto)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(bruto), "-ac", "2",
                        "-ar", "48000", str(destino)], check=True)
        bruto.unlink(missing_ok=True)
        if palavras and destino == wav:
            tempos.write_text(json.dumps(palavras, ensure_ascii=False), encoding="utf-8")
        wav = destino
    dur = len(ler_audio(wav)) / SR
    if not com_palavras:
        return wav, dur
    if not palavras:
        return wav, dur, _estimar_palavras(texto, dur)
    # a voz devolve as palavras sem pontuação; recoloca a partir do texto original
    tokens, j, alinhadas = texto.split(), 0, []
    limpa = lambda s: re.sub(r"\W", "", s.lower())
    for a, b, w in palavras:
        n = len(w.split())
        pedaco = tokens[j:j + n]
        if limpa("".join(pedaco)) == limpa(w):
            # a voz às vezes junta palavras ("5 saiu"): reparte o tempo entre elas
            total = sum(len(t) for t in pedaco) or 1
            t = a
            for tok in pedaco:
                d = (b - a) * len(tok) / total
                alinhadas.append((t, t + d, tok))
                t += d
            j += n
        else:
            alinhadas.append((a, b, w))
    return wav, dur, alinhadas


def legenda_da_narracao(palavras, inicio: float, cfg: dict) -> list:
    """Agrupa as palavras narradas em blocos curtos (estilo TikTok)."""
    maximo = cfg.get("legenda", {}).get("letras_por_bloco_narracao", 22)
    blocos, atual = [], []
    for p in palavras:
        texto = " ".join(w for _, _, w in atual + [p])
        fecha_frase = atual and atual[-1][2][-1:] in ".!?,"
        if atual and (len(texto) > maximo or fecha_frase):
            # não termina bloco com palavrinha solta ("de", "em", "no"): ela vai pro próximo
            sobra = []
            if not fecha_frase and len(atual) > 1 and len(atual[-1][2]) <= 3:
                sobra = [atual.pop()]
            blocos.append(atual)
            atual = sobra
        atual.append(p)
    if atual:
        blocos.append(atual)
    out = []
    for i, b in enumerate(blocos):
        a = b[0][0] + inicio
        fim = (blocos[i + 1][0][0] if i + 1 < len(blocos) else b[-1][1] + 0.3) + inicio
        palavras_abs = [(wa + inicio, wb + inicio, w) for wa, wb, w in b]
        out.append((a, fim, " ".join(w for _, _, w in b), palavras_abs))
    return out


def tem_audio(video: Path) -> bool:
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video)], capture_output=True,
                       text=True, encoding="utf-8", errors="ignore")
    return "Audio:" in r.stderr


def montar_narracao(gancho: str, duracao: float, cfg: dict, fala_inicio: str = ""):
    """Escolhe o que vai ser falado e quando. Retorna lista de (wav, inicio, dur, texto)."""
    narr = cfg.get("narracao", {})
    if not narr.get("ativar", False):
        return []
    falas = []
    abre = fala_inicio or (gancho if narr.get("falar_gancho_no_inicio", True) else "")
    if abre:
        wav, d = gerar_fala(abre, cfg)
        falas.append((wav, 0.4, d, abre))
    fins = narr.get("falas_fim") or []
    if fins:
        fecha = random.choice(fins)
        wav, d = gerar_fala(fecha, cfg)
        t = duracao - d - 0.5
        fim_da_abertura = falas[0][1] + falas[0][2] + 1.5 if falas else 0
        if t > fim_da_abertura:
            falas.append((wav, t, d, fecha))
    return falas


def expr_abaixar(falas, nivel: float, rampa: float = 0.25) -> str:
    """Expressão de volume do ffmpeg: abaixa o jogo pra `nivel` enquanto a voz fala."""
    expr = "1"
    for _, ini, dur, _ in falas:
        a, b = ini - 0.1, ini + dur + 0.1
        dentro = f"clip(min((t-{a - rampa:.2f})/{rampa},({b + rampa:.2f}-t)/{rampa}),0,1)"
        expr += f"*(1-{1 - nivel:.2f}*{dentro})"
    return expr


# ---------------------------------------------------------------- legenda (whisper.cpp)
WHISPER = BASE / "whisper"
URL_WHISPER = "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip"
URL_MODELO = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin"
URL_VAD = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin"
# frases que o whisper "inventa" em trechos só com música/barulho
ALUCINACOES = re.compile(
    r"^\W*(thanks? (you )?for watching|subscribe|legendas? (pela|por)|obrigad[oa] por assistir"
    r"|music|música|musica|applause|aplausos|risos|laughter|silence|silêncio|you)\W*$", re.I)


def _baixar(url: str, destino: Path, nome: str):
    print(f"   baixando {nome} ...", flush=True)
    tmp = destino.with_suffix(destino.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        feito = 0
        while bloco := r.read(1 << 20):
            f.write(bloco)
            feito += len(bloco)
            if total:
                print(f"\r   {nome}: {feito * 100 // total}%", end="", flush=True)
    print()
    tmp.replace(destino)


def achar_whisper_exe():
    achados = sorted(WHISPER.rglob("whisper-cli.exe"))
    return achados[0] if achados else None


def preparar_whisper(cfg) -> bool:
    """Garante whisper-cli.exe + modelo + VAD na pasta whisper/. Retorna True se ok."""
    leg = cfg.get("legenda", {})
    WHISPER.mkdir(exist_ok=True)
    modelo = WHISPER / f"ggml-{leg.get('modelo', 'small')}.bin"
    try:
        if not achar_whisper_exe():
            z = WHISPER / "whisper-bin-x64.zip"
            _baixar(URL_WHISPER, z, "whisper.cpp")
            with zipfile.ZipFile(z) as zz:
                zz.extractall(WHISPER)
            z.unlink()
        if not modelo.exists():
            _baixar(URL_MODELO.format(leg.get("modelo", "small")), modelo, modelo.name)
        vad = WHISPER / "ggml-silero-v5.1.2.bin"
        if not vad.exists():
            _baixar(URL_VAD, vad, vad.name)
    except Exception as e:
        print(f"   [não consegui baixar o whisper: {e}]")
        return False
    return achar_whisper_exe() is not None


def traduzir(texto: str, para: str) -> str:
    """Tradução grátis (Google Tradutor, sem chave). Se falhar, devolve o original."""
    try:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto"
               f"&tl={para}&dt=t&q={urllib.parse.quote(texto)}")
        with urllib.request.urlopen(url, timeout=15) as r:
            dados = json.loads(r.read().decode("utf-8"))
        return "".join(p[0] for p in dados[0] if p and p[0]).strip() or texto
    except Exception:
        return texto


def _tem_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401  (usado na nuvem/Linux, onde não tem o whisper.cpp)
        return True
    except ImportError:
        return False


def legenda_disponivel() -> bool:
    return bool(achar_whisper_exe()) or _tem_faster_whisper()


_FW = {}


def _transcrever_fw(wav: Path, leg: dict):
    """faster-whisper: devolve (idioma, [(inicio, fim, texto)])."""
    from faster_whisper import WhisperModel
    modelo = leg.get("modelo", "small")
    if modelo not in _FW:
        _FW[modelo] = WhisperModel(modelo, device="cpu", compute_type="int8")
    idioma = leg.get("idioma_do_jogo", "auto")
    segs, info = _FW[modelo].transcribe(str(wav), language=None if idioma == "auto" else idioma,
                                        vad_filter=True, beam_size=5)
    return info.language or "", [(s.start, s.end, s.text) for s in segs]


def transcrever(video: Path, ini: float, dur: float, cfg: dict):
    """Retorna lista de (inicio, fim, texto) das falas do jogo dentro do corte."""
    leg = cfg.get("legenda", {})
    exe = achar_whisper_exe()
    if not exe and not _tem_faster_whisper():
        return []
    TMP.mkdir(exist_ok=True)
    wav, saida = TMP / "fala_jogo.wav", TMP / "fala_jogo"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{ini:.2f}", "-t", f"{dur:.2f}",
                    "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    if not exe:
        idioma, segs = _transcrever_fw(wav, leg)
        return _montar_legendas(idioma, [(a, b, t) for a, b, t in segs], dur, leg)
    base = [str(exe), "-m", str(WHISPER / f"ggml-{leg.get('modelo', 'small')}.bin"),
            "-f", str(wav), "-l", leg.get("idioma_do_jogo", "auto"), "-oj", "-of", str(saida),
            "-np",
            "-t", str(max(2, (os.cpu_count() or 4) - 1))]
    vad = WHISPER / "ggml-silero-v5.1.2.bin"
    json_out = saida.with_suffix(".json")
    json_out.unlink(missing_ok=True)
    r = subprocess.run(base + ["--vad", "-vm", str(vad)] if vad.exists() else base,
                       capture_output=True)
    if r.returncode != 0 or not json_out.exists():  # versão sem VAD? tenta sem
        subprocess.run(base, capture_output=True)
    if not json_out.exists():
        return []
    dados = json.loads(json_out.read_text(encoding="utf-8", errors="ignore"))
    segs = [(s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s.get("text", ""))
            for s in dados.get("transcription", [])]
    return _montar_legendas(dados.get("result", {}).get("language", ""), segs, dur, leg)


def _montar_legendas(idioma: str, segs: list, dur: float, leg: dict):
    """Limpa, traduz se precisar e quebra em blocos curtos: [(inicio, fim, texto)]."""
    para = leg.get("traduzir_para", "")
    falas = []
    for a, b, bruto in segs:
        texto = re.sub(r"\[.*?\]|\(.*?\)|♪", "", bruto).strip()
        if len(texto) < 2 or ALUCINACOES.match(texto):
            continue
        if b - a < 0.4:
            b = a + 0.8
        falas.append([a, min(b, dur), texto])
    if para and idioma and not idioma.startswith(para):
        for f in falas:
            f[2] = traduzir(f[2], para)
    # frase longa -> vários blocos curtos na tela, tempo dividido pelo tamanho
    maximo = leg.get("max_letras_por_legenda", 52)
    blocos = []
    for a, b, texto in falas:
        n = -(-len(texto) // maximo)  # quantos blocos precisa, em tamanhos parecidos
        partes = textwrap.wrap(texto, width=max(len(texto) // n + 8, 12)) or [texto]
        total = sum(len(p) for p in partes)
        t = a
        for p in partes:
            fim = t + (b - a) * len(p) / total
            blocos.append((t, fim, p))
            t = fim
    return blocos


def _tempo_ass(s: float) -> str:
    cs = int(round(s * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _cor_ass(cor: str) -> str:
    """'&H0000E5FF' (estilo) -> '&H00E5FF&' (tag inline)."""
    return "&H" + cor.upper().replace("&H", "")[-6:] + "&"


def _quebrar(palavras: list, largura: int) -> list:
    """Divide palavras em linhas de até `largura` letras (índices por linha), deixando
    as linhas com tamanhos parecidos em vez de uma cheia e outra com uma palavra só."""
    total = len(" ".join(palavras))
    if total > largura:
        n = -(-total // largura)
        largura = max(max(len(w) for w in palavras), -(-total // n) + 2)
    linhas, atual, tam = [], [], 0
    for i, w in enumerate(palavras):
        if atual and tam + 1 + len(w) > largura:
            linhas.append(atual)
            atual, tam = [], 0
        tam += len(w) + (1 if atual else 0)
        atual.append(i)
    if atual:
        linhas.append(atual)
    return linhas


def escrever_ass(falas, destino: Path, cfg: dict, narracao: bool = False):
    """Legenda estilo TikTok: maiúsculas, contorno grosso, 'pop' na entrada e, na
    narração, a palavra que está sendo falada acende colorida (karaokê)."""
    leg = cfg.get("legenda", {})
    fonte = leg.get("fonte", "Arial Black")
    tam = leg.get("tamanho_fonte_narracao", 76) if narracao else leg.get("tamanho_fonte", 58)
    contorno = leg.get("contorno", 7)
    cab = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
        "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Fala,{fonte},{tam},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,"
        f"100,100,1,0,1,{contorno},4,2,50,50,{leg.get('altura_do_rodape', 430)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    pop = "\\fscx70\\fscy70\\t(0,110,\\fscx100\\fscy100)"
    cor_jogo = _cor_ass(leg.get("cor_destaque", "&H0000E5FF"))
    cor_palavra = _cor_ass(leg.get("cor_palavra_falada", "&H0000E5FF"))
    largura = leg.get("letras_por_linha_narracao", 14) if narracao else leg.get("letras_por_linha", 20)
    ev = []

    def dialogo(a, b, texto):
        ev.append(f"Dialogue: 0,{_tempo_ass(a)},{_tempo_ass(b)},Fala,,0,0,0,,{texto}")

    for item in falas:
        a, b, texto = item[:3]
        palavras = texto.upper().split()
        linhas = _quebrar(palavras, largura)
        tempos = item[3] if len(item) > 3 else None
        if not tempos or len(tempos) != len(palavras):
            # bloco inteiro de uma vez (falas do jogo): amarelo + pop
            corpo = "\\N".join(" ".join(palavras[i] for i in l) for l in linhas)
            dialogo(a, b, f"{{{pop}\\c{cor_jogo}}}{corpo}")
            continue
        # karaokê: um evento por palavra, com ela destacada
        for k in range(len(palavras)):
            ini = a if k == 0 else tempos[k][0]
            fim = tempos[k + 1][0] if k + 1 < len(palavras) else b
            # no 1º evento o bloco está "pulando"; mexer na escala ali cortaria a animação
            if k == 0:
                liga, desliga = f"{{\\c{cor_palavra}}}", "{\\c&HFFFFFF&}"
            else:
                liga, desliga = f"{{\\c{cor_palavra}\\fscx110\\fscy110}}", "{\\r}"
            partes = [" ".join(f"{liga}{palavras[i]}{desliga}" if i == k else palavras[i]
                               for i in l) for l in linhas]
            efeito = f"{{{pop}}}" if k == 0 else ""
            dialogo(ini, fim, efeito + "\\N".join(partes))
    destino.write_text(cab + "\n".join(ev) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- render
def esc(caminho: Path) -> str:
    """Escapa caminho de arquivo para dentro de filtro do ffmpeg."""
    return str(caminho).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def renderizar(video: Path, ini: float, fim: float, gancho: str, destino: Path, cfg: dict,
               fala_inicio: str = "", roteiro=None, credito: str = "") -> list:
    """Renderiza o corte. Retorna (textos narrados, legendas das falas do jogo).
    roteiro = (wav, dur, palavras, texto): narração contínua (modo curiosidade).
    credito = texto fixo logo acima do vídeo (ex.: "CLIPE: twitch.tv/fulano")."""
    TMP.mkdir(exist_ok=True)
    dur = fim - ini
    audio_jogo = tem_audio(video)
    ass = TMP / "legenda.ass"
    legendas = []
    if roteiro:
        wav, d, palavras, texto = roteiro
        falas = [(wav, 0.4, d, texto)]
        if cfg.get("legenda", {}).get("legendar_narracao", True):
            escrever_ass(legenda_da_narracao(palavras, 0.4, cfg), ass, cfg, narracao=True)
            legendas = [(0, 0, "")]  # só pra ligar o filtro de legenda
    else:
        falas = montar_narracao(gancho, dur, cfg, fala_inicio)
        if audio_jogo and cfg.get("legenda", {}).get("ativar", False) and legenda_disponivel():
            legendas = transcrever(video, ini, dur, cfg)
        if legendas:
            escrever_ass(legendas, ass, cfg)
    txt = TMP / "gancho.txt"
    linhas = textwrap.wrap(gancho.upper(), width=cfg["letras_por_linha"]) or [""]
    txt.write_text("\n".join(linhas), encoding="utf-8", newline="\n")
    perfil = TMP / "perfil.txt"
    perfil.write_text(cfg.get("seu_arroba", ""), encoding="utf-8", newline="\n")
    fonte = esc(Path(cfg["fonte"]))
    zoom = cfg["recorte_laterais"]  # 0.78 = mantém 78% da largura (dá mais destaque ao centro)
    texto_credito = ""
    if credito:
        arq_credito = TMP / "credito.txt"
        arq_credito.write_text(credito, encoding="utf-8", newline="\n")
        texto_credito = (f"drawtext=fontfile='{fonte}':textfile='{esc(arq_credito)}':"
                         "fontsize=40:fontcolor=0xFFE500:borderw=4:bordercolor=black:"
                         "x=(w-text_w)/2:y=535,")

    filtro = (
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=25:2,eq=brightness=-0.18[fundo];"
        f"[b]crop=iw*{zoom}:ih,scale=1080:-2[jogo];"
        "[fundo][jogo]overlay=(W-w)/2:(H-h)/2[v0];"
        f"[v0]drawtext=fontfile='{fonte}':textfile='{esc(txt)}':text_align=C:"
        "fontsize=78:fontcolor=white:borderw=7:bordercolor=black:line_spacing=12:"
        "x=(w-text_w)/2:y=520-text_h[v1];"
        f"[v1]{texto_credito}drawtext=fontfile='{fonte}':textfile='{esc(perfil)}':"
        "fontsize=44:fontcolor=white@0.85:borderw=4:bordercolor=black:"
        "x=(w-text_w)/2:y=h-360,fps=fps='min(source_fps,60)'"
        + (f",subtitles=filename='{esc(ass)}':fontsdir='{esc(Path(cfg['fonte']).parent)}'"
           if legendas else "")
        + "[v]"
    )

    # ---- áudio: jogo (abaixado enquanto a voz fala) + narração, normalizado
    entradas = ["-ss", f"{ini:.2f}", "-t", f"{dur:.2f}", "-i", str(video)]
    for wav, *_ in falas:
        entradas += ["-i", str(wav)]
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    narr = cfg.get("narracao", {})
    vozes = []
    for i, (_, t0, _, _) in enumerate(falas, 1):
        ms = int(t0 * 1000)
        filtro += (f";[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                   f"volume={narr.get('volume_da_voz', 1.0)},adelay={ms}|{ms}[f{i}]")
        vozes.append(f"[f{i}]")
    tem_som = True
    if audio_jogo:
        filtro += (";[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                   f"volume='{expr_abaixar(falas, narr.get('volume_do_jogo_na_fala', 0.3))}'"
                   ":eval=frame[jogo]")
        partes = ["[jogo]"] + vozes
    else:
        partes = vozes
    if len(partes) > 1:
        filtro += f";{''.join(partes)}amix=inputs={len(partes)}:normalize=0:duration=first,{norm}[a]"
    elif partes == ["[jogo]"]:
        filtro += f";[jogo]{norm}[a]"
    elif partes:
        filtro += f";{partes[0]}apad,{norm}[a]"
    else:
        tem_som = False

    cmd = [FFMPEG, "-v", "error", "-y", *entradas, "-filter_complex", filtro,
           "-map", "[v]", *(["-map", "[a]", "-c:a", "aac", "-b:a", "160k"] if tem_som else []),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-t", f"{dur:.2f}", "-movflags", "+faststart", str(destino)]
    subprocess.run(cmd, check=True)
    return [f[3] for f in falas], [l[2] for l in legendas if l[2]]


def escrever_legenda(destino: Path, gancho: str, cfg: dict, textos=((), ()), creditos: str = ""):
    falas, legendas = textos
    titulo = random.choice(cfg["titulos"]).replace("{gancho}", gancho)
    tags = " ".join(cfg["hashtags"])
    corpo = f"{titulo}\n\n{creditos}\n\n{tags}" if creditos else f"{titulo} {tags}"
    narrado = ("\n\nNARRAÇÃO:\n" + "\n".join(f"- {f}" for f in falas)) if falas else ""
    jogo = ("\n\nFALAS DO JOGO (legendadas):\n" + "\n".join(f"- {l}" for l in legendas)
            if legendas else "")
    destino.with_suffix(".txt").write_text(
        f"TÍTULO:\n{titulo}\n\nLEGENDA (copiar e colar):\n{corpo}{narrado}{jogo}\n",
        encoding="utf-8")


# ---------------------------------------------------------------- modos
def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def para_segundos(t: str) -> float:
    partes = [float(p) for p in t.strip().split(":")]
    seg = 0.0
    for p in partes:
        seg = seg * 60 + p
    return seg


def modo_auto(cfg, so_listar=False):
    videos = sorted(p for p in ENTRADA.iterdir() if p.suffix.lower() in EXTS)
    if not videos:
        print(f"Nenhum vídeo em {ENTRADA}. Coloque as gravações do PS5 lá.")
        return
    ganchos = cfg["ganchos"][:]
    random.shuffle(ganchos)
    g = 0
    for video in videos:
        print(f"\n>> Analisando {video.name} ...")
        duracao, momentos = achar_momentos(video, cfg)
        print(f"   duração {hms(duracao)} | {len(momentos)} momento(s) bons")
        for n, (ini, fim, pico, nota) in enumerate(momentos, 1):
            destino = SAIDA / f"{video.stem}_corte{n:02d}.mp4"
            print(f"   #{n:02d}  {hms(ini)} -> {hms(fim)}  (pico {hms(pico)}, força {nota:.1f})", end="")
            if so_listar:
                print()
                continue
            if destino.exists():
                print("  [já existe, pulando]")
                continue
            gancho = ganchos[g % len(ganchos)]
            g += 1
            print(f"  -> renderizando \"{gancho}\"", flush=True)
            falas = renderizar(video, ini, fim, gancho, destino, cfg)
            escrever_legenda(destino, gancho, cfg, falas)


def modo_manual(cfg):
    csv = BASE / "cortes.csv"
    linhas = [l for l in csv.read_text(encoding="utf-8").splitlines()[1:]
              if l.strip() and not l.startswith("#")]
    if not linhas:
        print("cortes.csv está vazio. Formato: arquivo;inicio;fim;texto;narracao")
        return
    for n, linha in enumerate(linhas, 1):
        arquivo, ini, fim, texto, fala = (c.strip() for c in (linha.split(";") + [""] * 5)[:5])
        video = ENTRADA / arquivo
        if not video.exists():
            print(f"   [linha {n}] não achei {video}")
            continue
        gancho = texto or random.choice(cfg["ganchos"])
        destino = SAIDA / f"manual_{n:02d}_{video.stem}.mp4"
        print(f">> {arquivo} {ini} -> {fim}  \"{gancho}\"", flush=True)
        falas = renderizar(video, para_segundos(ini), para_segundos(fim), gancho, destino,
                           cfg, fala_inicio=fala)
        escrever_legenda(destino, gancho, cfg, falas)


def modo_curiosidade(cfg):
    """Narra uma curiosidade (config.json > curiosidades) por cima dos melhores momentos."""
    videos = sorted(p for p in ENTRADA.iterdir() if p.suffix.lower() in EXTS)
    if not videos:
        print(f"Nenhum vídeo em {ENTRADA}.")
        return
    usadas_arq = SAIDA / ".curiosidades_usadas.txt"
    usadas = (set(usadas_arq.read_text(encoding="utf-8-sig").splitlines())
              if usadas_arq.exists() else set())
    fila = [(i, c) for i, c in enumerate(cfg.get("curiosidades", []), 1)
            if c["gancho"] not in usadas]
    if not fila:
        print("Todas as curiosidades do config.json já foram usadas. Escreva novas lá "
              f"(ou apague {usadas_arq.name} pra repetir).")
        return
    narr = cfg.get("narracao", {})
    fins = narr.get("falas_fim_curiosidade") or narr.get("falas_fim") or [""]
    for video in videos:
        if not fila:
            break
        print(f"\n>> Analisando {video.name} ...")
        duracao, momentos = achar_momentos(video, cfg)
        momentos.sort(key=lambda m: -m[3])  # mais agitados primeiro
        for n, (_, _, pico, _) in enumerate(momentos, 1):
            if not fila:
                break
            idx, c = fila.pop(0)
            texto = f"{c['gancho']} {c['texto']} {random.choice(fins)}".strip()
            print(f"   curiosidade {idx:02d} \"{c['gancho']}\"", end="", flush=True)
            wav, d, palavras = gerar_fala(texto, cfg, com_palavras=True)
            dur = min(59.0, max(12.0, d + 1.5), duracao)
            ini = min(max(0.0, pico - dur * 0.5), max(0.0, duracao - dur))
            if d + 1.5 > 59:
                print(f"  [texto longo: {d:.0f}s de fala, vai cortar no 59s]", end="")
            print(f"  ({dur:.0f}s, a partir de {hms(ini)})", flush=True)
            destino = SAIDA / f"{video.stem}_curiosidade{idx:02d}.mp4"
            textos = renderizar(video, ini, ini + dur, c.get("texto_tela", c["gancho"]), destino,
                                cfg, roteiro=(wav, d, palavras, texto))
            escrever_legenda(destino, c["gancho"], cfg, textos)
            with open(usadas_arq, "a", encoding="utf-8") as f:
                f.write(c["gancho"] + "\n")


# ---------------------------------------------------------------- clipes da Twitch
CLIPES = BASE / "clipes"
SEGREDOS = BASE / "segredos.json"
USADOS_TWITCH = SAIDA / ".clipes_usados.txt"


def _twitch_get(caminho: str, params, cab: dict) -> dict:
    url = f"https://api.twitch.tv/helix/{caminho}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


CHAVES_SEGREDOS = ["twitch_client_id", "twitch_client_secret", "telegram_token", "telegram_chat_id"]


def ler_segredos() -> dict:
    """Lê o segredos.json, criando (ou completando) os campos que faltarem."""
    seg = json.loads(SEGREDOS.read_text(encoding="utf-8-sig")) if SEGREDOS.exists() else {}
    if any(k not in seg for k in CHAVES_SEGREDOS):
        for k in CHAVES_SEGREDOS:
            seg.setdefault(k, "")
        salvar_segredos(seg)
    return seg


def salvar_segredos(seg: dict):
    SEGREDOS.write_text(json.dumps(seg, indent=2, ensure_ascii=False), encoding="utf-8")


def twitch_conectar():
    """Pega um token de app com o Client ID/Secret do segredos.json."""
    seg = ler_segredos()
    cid = seg.get("twitch_client_id", "").strip()
    secret = seg.get("twitch_client_secret", "").strip()
    if not cid or not secret:
        print(f"Preencha twitch_client_id e twitch_client_secret no arquivo {SEGREDOS}\n"
              "(crie o app grátis em https://dev.twitch.tv/console/apps).")
        return None
    dados = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                    "grant_type": "client_credentials"}).encode()
    try:
        with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", data=dados,
                                    timeout=20) as r:
            token = json.loads(r.read().decode("utf-8"))["access_token"]
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", errors="ignore")
        if "Web Filter" in corpo or corpo.lstrip().startswith("<"):
            print("A rede bloqueou a Twitch (filtro de internet, comum em rede de empresa). "
                  "Suas chaves podem estar certas: rode num PC/rede pessoal.")
        else:
            print(f"A Twitch recusou o login ({e.code}). Confira o Client ID e o Secret.")
        return None
    except urllib.error.URLError as e:
        print(f"Sem conexão com a Twitch: {e.reason}")
        return None
    return {"Client-Id": cid, "Authorization": f"Bearer {token}"}


def limpar_titulo(titulo: str, maximo: int = 60) -> str:
    """Tira emoji/símbolo (a fonte não desenha) e encurta sem cortar palavra."""
    t = re.sub(r"[^\w\s.,!?'\"\-:()%$#@&]", "", titulo)
    t = re.sub(r"\s+", " ", t).strip(" -|")
    if len(t) > maximo:
        t = t[:maximo].rsplit(" ", 1)[0] + "..."
    return t


def _paginar_clipes(params_base: list, cab: dict, filtro, paginas: int, quer: int) -> list:
    """Busca clipes (a Twitch já devolve do mais visto pro menos) aplicando o filtro."""
    achados, cursor = [], None
    for _ in range(paginas):
        params = params_base + [("first", 100)] + ([("after", cursor)] if cursor else [])
        r = _twitch_get("clips", params, cab)
        achados += [c for c in r["data"] if filtro(c)]
        cursor = r.get("pagination", {}).get("cursor")
        if not cursor or len(achados) >= quer:
            break
    return achados


def buscar_clipes(cfg: dict, cab: dict) -> list:
    """Junta: (1) os clipes mais vistos de GTA da Twitch inteira no idioma escolhido e
    (2) os dos streamers da lista. Cada clipe sai marcado com _autorizado True/False."""
    from datetime import datetime, timedelta, timezone
    tw = cfg["twitch"]
    desc = tw.get("descobrir", {})
    streamers = {s["login"].strip().lower(): s for s in tw.get("streamers", [])}
    autorizados = {l for l, s in streamers.items() if s.get("autorizado")}
    bloqueados = {l.strip().lower() for l in tw.get("nunca_pegar", [])}
    jogos = _twitch_get("games", [("name", n) for n in tw["jogos"]], cab)["data"]
    ids_jogos = {j["id"] for j in jogos}
    print("Jogos: " + (", ".join(j["name"] for j in jogos) or "nenhum achado na Twitch"))
    usados = (set(USADOS_TWITCH.read_text(encoding="utf-8").split())
              if USADOS_TWITCH.exists() else set())
    agora = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    periodo = lambda dias: [("started_at", (agora - timedelta(days=dias)).strftime(fmt)),
                            ("ended_at", agora.strftime(fmt))]
    idiomas = tuple(i.lower() for i in desc.get("idiomas", ["pt"]))

    proibidos = [p.lower() for p in tw.get("bloquear_no_titulo", [])]
    rp = [p.lower() for p in tw.get("bloquear_live_com", [])]

    def live_ok(s):
        """Live na categoria GTA 6 mas jogando RP de GTA 5 (título com 'RP', 'cidade'...)."""
        t = f" {re.sub(r'[^\w]+', ' ', (s.get('title') or '').lower())} "
        ruim = next((p for p in rp if f" {p} " in t or (len(p) > 4 and p in t)), None)
        if ruim:
            print(f"   (ignorando {s['user_name']}: título da live tem \"{ruim}\" -> provável GTA 5 RP)")
        return not ruim

    def titulo_ok(c):
        t = f" {(c.get('title') or '').lower()} "
        return not any(p in t for p in proibidos)

    def bom(c, minimo, qualquer_jogo=False):
        return ((qualquer_jogo or c["game_id"] in ids_jogos) and c["id"] not in usados
                and titulo_ok(c)
                and c["broadcaster_name"].lower() not in bloqueados
                and c["view_count"] >= minimo
                and c["duration"] >= tw.get("min_segundos", 8))

    escolhidos = {}
    # (1) os virais da Twitch inteira: dos jogos configurados ou, com "todos_os_jogos",
    #     dos jogos/categorias mais assistidos agora (a Twitch não deixa buscar "tudo" de uma vez)
    if desc.get("ativar", True):
        minimo = desc.get("min_visualizacoes", tw.get("min_visualizacoes", 0))
        geral = desc.get("todos_os_jogos", False)
        busca_ids = set(ids_jogos)
        if geral:
            top = _twitch_get("games/top", [("first", desc.get("top_jogos", 15))], cab)["data"]
            busca_ids |= {g["id"] for g in top}
            print("   Categorias em alta: " + ", ".join(g["name"] for g in top))
        filtro = lambda c: (bom(c, minimo, geral) and
                            (not idiomas or (c.get("language") or "").lower().startswith(idiomas)))
        achados = []
        for gid in busca_ids:
            achados += _paginar_clipes([("game_id", gid)] + periodo(desc.get("dias", 3)), cab,
                                       filtro, desc.get("paginas", 5), 999)
        achados.sort(key=lambda c: -c["view_count"])
        por_canal = {}
        for c in achados:
            canal = c["broadcaster_name"].lower()
            if por_canal.get(canal, 0) >= desc.get("max_por_streamer", 2):
                continue  # variedade: não deixa um streamer só dominar a lista
            por_canal[canal] = por_canal.get(canal, 0) + 1
            escolhidos[c["id"]] = c
            if len(escolhidos) >= desc.get("max_clipes", 10):
                break
        print(f"   Virais {'gerais' if geral else 'do jogo'} ({'/'.join(idiomas) or 'todos os idiomas'}): "
              f"{len(escolhidos)} clipe(s) novo(s)")
    # (1b) quem está AO VIVO agora no jogo: clipes que o chat fez nas últimas horas
    vivo = tw.get("ao_vivo", {})
    if vivo.get("ativar", True) and ids_jogos:
        # cada grupo: idiomas, mínimo de espectadores e quantas lives (brasileiros primeiro)
        grupos = vivo.get("grupos", [{"nome": "BR", "idiomas": ["pt"], "min_espectadores": 300, "max_lives": 15}])
        lives, vistos = [], set()
        for g in grupos:
            params = [("game_id", j) for j in ids_jogos] + [("first", 100)]
            params += [("language", i) for i in g.get("idiomas", [])]  # [] = qualquer idioma
            achadas = [s for s in _twitch_get("streams", params, cab)["data"]
                       if s["viewer_count"] >= g.get("min_espectadores", 0) and s["user_id"] not in vistos
                       and live_ok(s)]
            achadas.sort(key=lambda s: -s["viewer_count"])
            achadas = achadas[:g.get("max_lives", 10)]
            vistos |= {s["user_id"] for s in achadas}
            lives += achadas
            print(f"   Ao vivo agora ({g.get('nome', '?')}): {len(achadas)} live(s)"
                  + (" - " + ", ".join(f"{s['user_name']} ({s['viewer_count']})" for s in achadas[:10])
                     if achadas else ""))
        antes = len(escolhidos)
        for s in lives:
            achados = _paginar_clipes(
                [("broadcaster_id", s["user_id"])] + periodo(vivo.get("horas", 8) / 24),
                cab, lambda c: bom(c, vivo.get("min_visualizacoes", 20)), 2,
                vivo.get("clipes_por_live", 2))
            achados.sort(key=lambda c: -c["view_count"])
            for c in achados[:vivo.get("clipes_por_live", 2)]:
                escolhidos[c["id"]] = c
        print(f"   Clipes das lives de agora: {len(escolhidos) - antes}")
    # (2) os streamers da lista: todos (os sem "liberou" chegam com aviso pra você decidir),
    #     e de qualquer jogo se "streamers_qualquer_jogo" estiver ligado
    lista = set(streamers) - bloqueados
    if tw.get("so_autorizados", False):
        lista &= autorizados
    qualquer = tw.get("streamers_qualquer_jogo", True)
    if lista:
        usuarios = _twitch_get("users", [("login", l) for l in lista], cab)["data"]
        for l in lista - {u["login"] for u in usuarios}:
            print(f"   [não achei o canal twitch.tv/{l}; confira o login no config.json]")
        # quem da lista está ao vivo jogando RP (título da live) fica de fora nesta rodada
        no_ar = _twitch_get("streams", [("user_id", u["id"]) for u in usuarios[:100]], cab)["data"] if usuarios else []
        em_rp = {s["user_id"] for s in no_ar if not live_ok(s)}
        usuarios = [u for u in usuarios if u["id"] not in em_rp]
        for u in usuarios:
            quer = tw.get("clipes_por_streamer", 3)
            achados = _paginar_clipes([("broadcaster_id", u["id"])] + periodo(tw.get("dias", 7)),
                                      cab, lambda c: bom(c, tw.get("min_visualizacoes", 0), qualquer),
                                      5, quer)
            achados.sort(key=lambda c: -c["view_count"])
            for c in achados[:quer]:
                escolhidos[c["id"]] = c
            print(f"   {u['display_name']}: {len(achados)} clipe(s) novo(s)"
                  f"{'' if qualquer else ' de GTA'}")
    # o clipe só traz o nome de exibição; o login (o do link twitch.tv/...) vem por id
    ids = list({c["broadcaster_id"] for c in escolhidos.values()})
    logins = {}
    for i in range(0, len(ids), 100):
        for u in _twitch_get("users", [("id", x) for x in ids[i:i + 100]], cab)["data"]:
            logins[u["id"]] = u["login"]
    for c in escolhidos.values():
        c["_login"] = logins.get(c["broadcaster_id"], c["broadcaster_name"].lower())
        c["_autorizado"] = c["_login"] in autorizados
    # brasileiros (e os da sua lista) primeiro; depois os gringos, cada grupo do mais visto pro menos
    br = lambda c: (c.get("language") or "").lower().startswith("pt") or c["_login"] in streamers
    return sorted(escolhidos.values(), key=lambda c: (not br(c), -c["view_count"]))


def baixar_clipe(clipe: dict):
    CLIPES.mkdir(exist_ok=True)
    destino = CLIPES / (re.sub(r"[^\w-]", "_", clipe["id"]) + ".mp4")
    if destino.exists():
        return destino
    r = subprocess.run([*YTDLP,"-q", "--no-warnings",
                        "-f", "best[ext=mp4]/best", "-o", str(destino), clipe["url"]],
                       capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0 or not destino.exists():
        print(f"   [não consegui baixar: {(r.stderr or '').strip()[-200:]}]")
        return None
    return destino


# ---------------------------------------------------------------- aprovação pelo Telegram
APROVADOS = SAIDA / "aprovados"
RECUSADOS = SAIDA / "recusados"
PENDENTES_TG = SAIDA / ".telegram_pendentes.json"


def _tg(token: str, metodo: str, dados: dict = None, arquivo=None, timeout: int = 60) -> dict:
    """Chama a API do bot. arquivo = (campo, Path) pra mandar vídeo (multipart)."""
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    if arquivo:
        limite = "----vicediario" + hashlib.md5(os.urandom(8)).hexdigest()
        corpo = b""
        for k, v in (dados or {}).items():
            corpo += (f"--{limite}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                      f"{v}\r\n").encode("utf-8")
        campo, caminho = arquivo
        corpo += (f"--{limite}\r\nContent-Disposition: form-data; name=\"{campo}\"; "
                  f"filename=\"{caminho.name}\"\r\nContent-Type: video/mp4\r\n\r\n").encode("utf-8")
        corpo += caminho.read_bytes() + f"\r\n--{limite}--\r\n".encode("utf-8")
        req = urllib.request.Request(url, data=corpo, headers={
            "Content-Type": f"multipart/form-data; boundary={limite}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(dados or {}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}


def telegram_conectar(avisar: bool = True):
    """Devolve (token, chat_id) ou None. Descobre o chat_id sozinho na 1ª vez."""
    seg = ler_segredos()
    token = str(seg.get("telegram_token", "")).strip()
    if not token:
        if avisar:
            print(f"Preencha telegram_token em {SEGREDOS} (crie o bot no @BotFather).")
        return None
    chat = str(seg.get("telegram_chat_id", "")).strip()
    if not chat:
        r = _tg(token, "getUpdates", {"timeout": 0})
        if not r.get("ok"):
            print(f"Telegram recusou o token: {r.get('description')}")
            return None
        chats = [u["message"]["chat"]["id"] for u in r["result"] if "message" in u]
        if not chats:
            print("Abra o seu bot no Telegram, mande /start pra ele e rode de novo.")
            return None
        chat = str(chats[-1])
        seg["telegram_chat_id"] = chat
        salvar_segredos(seg)
        _tg(token, "sendMessage", {"chat_id": chat, "text": "✅ Bot conectado! "
                                   "Os vídeos pra aprovar vão chegar aqui."})
    return token, chat


# rodando no GitHub Actions: não tem pasta pra mover; os botões só marcam a decisão
NUVEM = bool(os.environ.get("FABRICA_NUVEM"))


def telegram_enviar_pra_aprovar(tg, video: Path, legenda: str):
    token, chat = tg
    if video.stat().st_size > 49 * 1024 * 1024:
        print("   [vídeo passou de 50 MB, o bot do Telegram não aceita; aprove pela pasta]")
        return
    chave = hashlib.md5(video.name.encode("utf-8")).hexdigest()[:10]
    p = "n" if NUVEM else ""
    botoes = {"inline_keyboard": [[{"text": "✅ Aprovar", "callback_data": f"{p}ok:{chave}"},
                                   {"text": "❌ Recusar", "callback_data": f"{p}no:{chave}"}]]}
    print("   -> enviando pro Telegram ...", end="", flush=True)
    r = _tg(token, "sendVideo", {"chat_id": chat, "caption": legenda[:1000],
                                 "supports_streaming": "true",
                                 "reply_markup": json.dumps(botoes)},
            arquivo=("video", video), timeout=300)
    if not r.get("ok"):
        print(f" falhou: {r.get('description')}")
        return
    print(" ok")
    # logo abaixo, a legenda pronta: no Telegram, tocar no bloco copia o texto inteiro
    _, texto = _ler_txt(video.with_suffix(".txt"))
    if texto:
        seguro = texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        _tg(token, "sendMessage", {"chat_id": chat, "parse_mode": "HTML",
                                   "reply_to_message_id": r["result"]["message_id"],
                                   "text": f"📋 <b>Legenda pronta</b> (toque pra copiar):\n<pre>{seguro[:3800]}</pre>"})
    if not NUVEM:
        pend = json.loads(PENDENTES_TG.read_text(encoding="utf-8")) if PENDENTES_TG.exists() else {}
        pend[chave] = video.name
        PENDENTES_TG.write_text(json.dumps(pend, indent=1), encoding="utf-8")


def _tg_marcar(token: str, chat: str, q: dict, aprovado: bool) -> str:
    status = "✅ APROVADO" if aprovado else "❌ RECUSADO"
    _tg(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": status})
    _tg(token, "editMessageReplyMarkup", {
        "chat_id": chat, "message_id": q["message"]["message_id"],
        "reply_markup": {"inline_keyboard": [[{"text": status, "callback_data": "x:x"}]]}})
    return status


def baixar_aprovado_da_nuvem(token: str, msg: dict, cfg: dict):
    """Vídeo feito na nuvem e aprovado no Telegram: baixa pro saida/aprovados com a legenda."""
    video = msg.get("video") or msg.get("document")
    if not video:
        return None
    APROVADOS.mkdir(parents=True, exist_ok=True)
    destino = APROVADOS / f"nuvem_{msg['message_id']}.mp4"
    if destino.exists():
        return destino
    if (video.get("file_size") or 0) > 20 * 1024 * 1024:
        print("   [vídeo com mais de 20 MB: o Telegram não deixa o bot baixar; salve pelo celular]")
        return None
    r = _tg(token, "getFile", {"file_id": video["file_id"]})
    if not r.get("ok"):
        print(f"   [não consegui baixar do Telegram: {r.get('description')}]")
        return None
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{r['result']['file_path']}",
                                timeout=300) as resp:
        destino.write_bytes(resp.read())
    # a legenda vem da própria mensagem: "🎬 Nome | 👁 views | ...", "título", link
    cap = msg.get("caption") or ""
    linhas = [l.strip() for l in cap.splitlines() if l.strip()]
    nome = linhas[0].replace("🎬", "").split("|")[0].strip() if linhas else ""
    titulo = next((l.strip('"') for l in linhas[1:] if l.startswith('"')), "") or nome
    link = next((l for l in linhas if l.startswith("http")), "")
    login = re.sub(r"\W", "", nome.lower())
    canal = f"https://twitch.tv/{login}" if "twitch" in link or "clips" in link else link
    tags = " ".join(cfg.get("twitch", {}).get("hashtags", cfg.get("hashtags", [])) + ([f"#{login}"] if login else []))
    arroba = cfg.get("seu_arroba", "")
    legenda = (f"{titulo} 😳\n\n⭐⭐⭐ Segue a {arroba} pra ver os melhores momentos de GTA 6 todo dia!\n"
               f"💬 Comenta o que você achou\n\n🎥 Créditos: {nome} - {canal}\n"
               f"Clipe original: {link}\n\n{tags}")
    destino.with_suffix(".txt").write_text(
        f"TÍTULO:\n{titulo}\n\nLEGENDA (copiar e colar):\n{legenda}\n", encoding="utf-8")
    destino.with_suffix(".json").write_text(json.dumps(
        {"plataforma": "twitch", "streamer": nome, "login": login, "canal_url": canal, "url": link,
         "titulo_original": titulo, "autorizado": False, "da_nuvem": True}, ensure_ascii=False), encoding="utf-8")
    print(f"   ⬇ baixado pro painel: {destino.name} ({nome})")
    return destino


def telegram_respostas_nuvem():
    """Na nuvem: marca no Telegram os vídeos que você aprovou/recusou desde a última rodada."""
    tg = telegram_conectar(avisar=False)
    if not tg:
        return
    token, chat = tg
    arq = SAIDA / ".tg_offset"
    offset = int(arq.read_text().strip() or 0) if arq.exists() else 0
    r = _tg(token, "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]})
    n = 0
    for u in r.get("result", []):
        q = u.get("callback_query")
        dado = (q or {}).get("data", "")
        if q and dado.startswith(("ok:", "no:", "x:")):
            break  # clique pro painel do PC (vídeo local ou "baixar de novo"): deixa pra ele
        offset = u["update_id"] + 1
        if q and str(q["message"]["chat"]["id"]) == chat and dado[:3] in ("nok", "nno"):
            _tg_marcar(token, chat, q, dado.startswith("nok"))
            n += 1
    arq.write_text(str(offset))
    print(f"Telegram: {n} decisão(ões) marcadas")


def mover_video(nome: str, pasta: Path) -> bool:
    """Move o vídeo (e o .txt/.json dele) pra pasta, venha de onde vier. Tira da fila do Telegram."""
    pasta.mkdir(parents=True, exist_ok=True)
    stem, moveu = Path(nome).stem, False
    for origem in (SAIDA, APROVADOS, RECUSADOS):
        if origem == pasta:
            continue
        for ext in (".mp4", ".txt", ".json"):
            arq = origem / (stem + ext)
            if arq.exists():
                arq.replace(pasta / arq.name)
                moveu = True
    if PENDENTES_TG.exists():
        pend = json.loads(PENDENTES_TG.read_text(encoding="utf-8"))
        resto = {k: v for k, v in pend.items() if Path(v).stem != stem}
        if resto != pend:
            PENDENTES_TG.write_text(json.dumps(resto, indent=1), encoding="utf-8")
    return moveu


def modo_aprovar():
    """Fica ouvindo os botões do Telegram e move os vídeos pra aprovados/ ou recusados/."""
    tg = telegram_conectar()
    if not tg:
        return
    token, chat = tg
    APROVADOS.mkdir(exist_ok=True)
    RECUSADOS.mkdir(exist_ok=True)
    seg = ler_segredos()
    offset = int(seg.get("_telegram_offset") or 0)
    print("Esperando você aprovar no Telegram... (Ctrl+C pra parar)")
    try:
        while True:
            r = _tg(token, "getUpdates", {"offset": offset, "timeout": 30,
                                          "allowed_updates": ["callback_query"]}, timeout=40)
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                q = u.get("callback_query")
                if not q or str(q["message"]["chat"]["id"]) != chat:
                    continue
                acao, chave = (q.get("data", "") + ":").split(":")[:2]
                # vídeo que veio da nuvem: marca e, se aprovado, baixa pro painel.
                # "x" = tocou de novo no botão "✅ APROVADO" (recupera um aprovado antigo)
                if acao in ("nok", "nno") or (acao == "x" and "APROVADO" in json.dumps(
                        q["message"].get("reply_markup", {}), ensure_ascii=False)):
                    if acao != "x":
                        print(f"   {_tg_marcar(token, chat, q, acao == 'nok')} (vídeo da nuvem)")
                    else:
                        _tg(token, "answerCallbackQuery", {"callback_query_id": q["id"],
                                                           "text": "Baixando pro painel..."})
                    if acao in ("nok", "x"):
                        try:
                            baixar_aprovado_da_nuvem(token, q["message"], carregar_config())
                        except Exception as e:  # noqa: BLE001
                            print(f"   [erro ao baixar da nuvem: {e}]")
                    continue
                pend = (json.loads(PENDENTES_TG.read_text(encoding="utf-8"))
                        if PENDENTES_TG.exists() else {})
                nome = pend.pop(chave, None)
                if not nome:
                    _tg(token, "answerCallbackQuery", {"callback_query_id": q["id"],
                                                       "text": "Esse já foi resolvido."})
                    continue
                PENDENTES_TG.write_text(json.dumps(pend, indent=1), encoding="utf-8")
                mover_video(nome, APROVADOS if acao == "ok" else RECUSADOS)
                print(f"   {_tg_marcar(token, chat, q, acao == 'ok')}: {nome}")
            seg = ler_segredos()
            seg["_telegram_offset"] = offset
            salvar_segredos(seg)
    except KeyboardInterrupt:
        print("\nParei. Aprovados em:", APROVADOS)


# ---------------------------------------------------------------- painel (site local)
REDES = ["youtube", "tiktok", "instagram", "facebook"]
ESTADO_REDES = SAIDA / ".redes.json"


def _ler_txt(txt: Path):
    if not txt.exists():
        return "", ""
    t = txt.read_text(encoding="utf-8", errors="ignore")
    titulo = t.split("TÍTULO:\n", 1)[-1].split("\n\n", 1)[0].strip() if "TÍTULO:" in t else ""
    leg = t.split("LEGENDA (copiar e colar):\n", 1)[-1] if "LEGENDA" in t else ""
    leg = re.split(r"\n\n(NARRAÇÃO|FALAS DO JOGO)", leg)[0].strip()
    return titulo, leg


def listar_videos() -> list:
    estado = json.loads(ESTADO_REDES.read_text(encoding="utf-8")) if ESTADO_REDES.exists() else {}
    out = []
    for status, pasta in (("pendente", SAIDA), ("aprovado", APROVADOS), ("recusado", RECUSADOS)):
        if not pasta.exists():
            continue
        for mp4 in sorted(pasta.glob("*.mp4"), key=lambda p: -p.stat().st_mtime):
            titulo, legenda = _ler_txt(mp4.with_suffix(".txt"))
            info = (json.loads(mp4.with_suffix(".json").read_text(encoding="utf-8"))
                    if mp4.with_suffix(".json").exists() else {})
            out.append({"nome": mp4.name, "status": status,
                        "url": f"/video/{status}/{urllib.parse.quote(mp4.name)}",
                        "titulo": titulo, "legenda": legenda, "info": info,
                        "criado": mp4.stat().st_mtime, "mb": round(mp4.stat().st_size / 1e6, 1),
                        "redes": {r: estado.get(mp4.stem, {}).get(r, "a_postar") for r in REDES},
                        "postado_em": {r: estado.get(mp4.stem, {}).get(f"_t_{r}") for r in REDES}})
    return out


def modo_painel():
    """O painel fica em painel.py (servidor) + pasta painel/ (a página)."""
    import painel
    painel.iniciar(sys.modules[__name__])


def modo_twitch(cfg, so_listar=False):
    if "twitch" not in cfg:
        print('Falta o bloco "twitch" no config.json.')
        return
    if not so_listar:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            print("Falta o yt-dlp. Rode:  python -m pip install yt-dlp")
            return
    cab = twitch_conectar()
    if not cab:
        return
    tw = cfg["twitch"]
    clipes = buscar_clipes(cfg, cab)
    if not clipes:
        print("Nenhum clipe novo. Aumente 'dias' ou diminua 'min_visualizacoes' no config.")
        return
    # no modo Twitch o vídeo é do streamer: config própria pra recorte, voz e títulos
    base = json.loads(json.dumps(cfg))
    base["recorte_laterais"] = tw.get("recorte_laterais", 1.0)
    base.setdefault("narracao", {}).update(tw.get("narracao", {}))
    base["hashtags"] = tw.get("hashtags", cfg["hashtags"])
    tg = telegram_conectar(avisar=False) if not so_listar else None
    for c in clipes:
        nome, login = c["broadcaster_name"], c["_login"]
        selo = "" if c["_autorizado"] else "  [sem autorização confirmada]"
        print(f"\n>> {nome} | {c['view_count']} views | {c['duration']:.0f}s | \"{c['title']}\"{selo}")
        print(f"   {c['url']}")
        if so_listar:
            continue
        video = baixar_clipe(c)
        if not video:
            continue
        gancho = limpar_titulo(c["title"]) if tw.get("usar_titulo_do_clipe", True) else ""
        if len(gancho.replace(" ", "")) < 8:
            gancho = ""  # título tipo "b" ou "kkk": usa um dos ganchos prontos do config
        cfg_c = dict(base)
        cfg_c["titulos"] = [t.replace("{streamer}", nome) for t in tw.get("titulos", cfg["titulos"])]
        cfg_c["hashtags"] = base["hashtags"] + [f"#{re.sub(r'\W', '', login)}"]
        finalizar_corte(
            cfg_c, video, 0.0, float(c["duration"]), gancho,
            SAIDA / f"twitch_{re.sub(r'\W', '', login)}_{re.sub(r'[^\w-]', '_', c['id'])}.mp4",
            credito=f"CLIPE: twitch.tv/{login}",
            creditos=(f"🎥 Créditos: {nome} - https://twitch.tv/{login}\n"
                      f"Clipe original: {c['url']}\nClipado por: {c['creator_name']}"),
            info={"plataforma": "twitch", "streamer": nome, "login": login,
                  "canal_url": f"https://twitch.tv/{login}", "views": c["view_count"],
                  "url": c["url"], "titulo_original": c["title"], "autorizado": c["_autorizado"],
                  "clipe_criado_em": c.get("created_at", "")},
            usados=(USADOS_TWITCH, c["id"]), tg=tg,
            legenda_tg=f"🎬 {nome} | 👁 {c['view_count']} views | {c['duration']:.0f}s\n"
                       f"\"{c['title']}\"\n{c['url']}")
    if tg:
        print("\nVídeos enviados pro Telegram pra você aprovar.")


def finalizar_corte(cfg_c, video: Path, ini: float, fim: float, gancho: str, destino: Path,
                    credito: str, creditos: str, info: dict, usados, tg=None, legenda_tg=""):
    """Parte comum de Twitch e YouTube: edita, escreve .txt/.json, marca como usado
    e manda pro Telegram aprovar."""
    gancho = gancho or random.choice(cfg_c["ganchos"])
    print(f"   -> renderizando \"{gancho}\"", flush=True)
    textos = renderizar(video, ini, fim, gancho, destino, cfg_c, credito=credito)
    escrever_legenda(destino, gancho, cfg_c, textos, creditos)
    destino.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    arq, chave = usados
    with open(arq, "a", encoding="utf-8") as f:
        f.write(chave + "\n")
    if tg:
        aviso = "" if info.get("autorizado") else "\n⚠️ Criador SEM autorização confirmada"
        telegram_enviar_pra_aprovar(tg, destino, legenda_tg + aviso)


# ---------------------------------------------------------------- vídeos virais do YouTube
USADOS_YOUTUBE = SAIDA / ".youtube_usados.txt"
# filtro da busca do YouTube: "ordenar por visualizações" + "enviado em" (hoje/semana/mês)
FILTRO_YT = {1: "CAMSAggC", 7: "CAMSAggD", 30: "CAMSAggE"}


def _ytdlp_json(args: list, timeout: int = 120):
    r = subprocess.run([*YTDLP,"-J", "--no-warnings", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="ignore",
                       timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        if "CERTIFICATE_VERIFY_FAILED" in (r.stderr or ""):
            return None, ("a rede está interceptando a conexão com o YouTube "
                          "(comum em rede de empresa): rode num PC/rede pessoal")
        erro = (r.stderr or "").strip().splitlines()
        return None, (erro[-1] if erro else "sem resposta")
    return json.loads(r.stdout), ""


def momentos_virais(heatmap: list, duracao: float, quantos: int, segundos: float) -> list:
    """Pelos 'momentos mais repetidos' do YouTube, devolve [(inicio, fim, força)]."""
    pontos = [h for h in heatmap or [] if h.get("start_time", 0) >= max(60, duracao * 0.03)]
    pontos.sort(key=lambda h: -h.get("value", 0))
    escolhidos = []
    for h in pontos:
        if len(escolhidos) >= quantos or h.get("value", 0) < 0.35:
            break
        meio = (h["start_time"] + h["end_time"]) / 2
        if any(abs(meio - (a + b) / 2) < segundos * 3 for a, b, _ in escolhidos):
            continue  # muito perto de um corte que já foi escolhido
        ini = min(max(0.0, meio - segundos * 0.6), max(0.0, duracao - segundos))
        escolhidos.append((ini, min(duracao, ini + segundos), float(h["value"])))
    return escolhidos


def buscar_youtube(cfg: dict) -> list:
    """Vídeos longos de GTA mais vistos (busca + canais escolhidos), já com os trechos virais."""
    from datetime import datetime, timedelta
    yt = cfg["youtube"]
    dias = yt.get("dias", 7)
    filtro = FILTRO_YT[min(FILTRO_YT, key=lambda d: abs(d - dias))]
    canais = {c["canal"].strip().lower(): c for c in yt.get("canais", []) if c.get("canal")}
    bloqueados = {c.strip().lower() for c in yt.get("nunca_pegar", [])}
    candidatos = {}
    # fila: links salvos à mão (entram primeiro, sem filtro de data nem de views)
    for link in yt.get("fila", []):
        m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", link)
        if m:
            candidatos[m.group(1)] = "fila"
    if candidatos:
        print(f"   fila: {len(candidatos)} vídeo(s) salvos")
    for q in yt.get("buscas", []):
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q)}&sp={filtro}"
        d, erro = _ytdlp_json(["--flat-playlist", "--playlist-end", "15", url])
        if not d:
            print(f"   [busca \"{q}\" falhou: {erro}]")
            continue
        for e in d.get("entries") or []:
            if e and e.get("id"):
                candidatos.setdefault(e["id"], "busca")
        print(f"   busca \"{q}\": {len(d.get('entries') or [])} vídeo(s)")
    for canal in canais:
        nome_url = canal if canal.startswith(("http", "@")) else "@" + canal
        base_url = nome_url if nome_url.startswith("http") else f"https://www.youtube.com/{nome_url}"
        achou, erros = 0, []
        for aba in ("streams", "videos"):
            d, erro = _ytdlp_json(["--flat-playlist", "--playlist-end", "6", f"{base_url}/{aba}"])
            erros.append(erro)
            for e in (d or {}).get("entries") or []:
                if e and e.get("id"):
                    candidatos.setdefault(e["id"], canal)
                    achou += 1
        rede = next((e for e in erros if "rede" in e), "")
        print(f"   canal {canal}: {achou} vídeo(s) recentes" if achou
              else f"   [canal {canal}: {rede}]" if rede
              else f"   [não achei o canal {canal}; confira o @ no config]")

    usados = (set(USADOS_YOUTUBE.read_text(encoding="utf-8").split())
              if USADOS_YOUTUBE.exists() else set())
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y%m%d")
    escolhidos, por_canal = [], {}
    motivos, primeiro_erro = {}, ""
    pula = lambda m: motivos.__setitem__(m, motivos.get(m, 0) + 1)
    print(f"   analisando {len(candidatos)} vídeo(s)...", flush=True)
    segundos = yt.get("segundos_por_corte", 45)
    for vid, origem in candidatos.items():
        info, erro = _ytdlp_json(["--skip-download", f"https://www.youtube.com/watch?v={vid}"])
        if not info:
            pula("o YouTube não deixou ler o vídeo")
            primeiro_erro = primeiro_erro or erro
            continue
        handle = (info.get("uploader_id") or info.get("channel_id") or "").lower()
        if handle in bloqueados or (info.get("channel") or "").lower() in bloqueados:
            pula("canal em 'nunca pegar'")
            continue
        exigir = [t.lower().replace(" ", "") for t in yt.get("exigir_no_titulo", [])]
        if exigir and not any(t in (info.get("title") or "").lower().replace(" ", "") for t in exigir):
            pula("título não fala do jogo escolhido")
            continue
        idioma = (info.get("language") or "").lower()
        if idioma and not idioma.startswith("pt"):
            pula("não é em português")
            continue
        if origem != "fila" and (info.get("upload_date") or "99999999") < limite:
            pula("mais antigo que o limite de dias")
            continue
        if (info.get("duration") or 0) < yt.get("min_duracao_min", 20) * 60:
            pula("curto demais (provável corte de outro canal)")
            continue
        if origem == "busca" and (info.get("view_count") or 0) < yt.get("min_visualizacoes", 0):
            pula("poucas views")
            continue
        momentos = momentos_virais(info.get("heatmap"), info["duration"],
                                   yt.get("cortes_por_video", 1), segundos)
        if not momentos and origem == "fila":
            # sem o gráfico de "mais repetidos": pega um trecho a 1/3 do vídeo
            ini = info["duration"] / 3
            momentos = [(ini, ini + segundos, 0.0)]
        cortes = [m for m in momentos if f"{vid}@{int(m[0]) // 30}" not in usados]
        if not cortes:
            pula("sem gráfico de mais repetidos" if not momentos else "trecho já usado")
            continue
        if origem != "fila" and por_canal.get(handle, 0) >= yt.get("max_por_canal", 2):
            pula("limite por canal")
            continue
        por_canal[handle] = por_canal.get(handle, 0) + 1
        conf = canais.get(handle) or canais.get((info.get("channel") or "").lower()) or {}
        info["_autorizado"] = bool(conf.get("autorizado"))
        info["_cortes"] = cortes
        info["_fila"] = origem == "fila"
        escolhidos.append(info)
    for m, n in sorted(motivos.items(), key=lambda x: -x[1]):
        print(f"   descartados: {n} - {m}")
    if primeiro_erro:
        print(f"   erro do YouTube: {primeiro_erro[:200]}")
    # os da fila vêm primeiro e não contam no limite; depois os mais vistos da busca
    fila = sorted([i for i in escolhidos if i["_fila"]], key=lambda i: -(i.get("view_count") or 0))
    resto = sorted([i for i in escolhidos if not i["_fila"]], key=lambda i: -(i.get("view_count") or 0))
    return fila + resto[:yt.get("max_videos", 6)]


def baixar_trecho(info: dict, ini: float, fim: float):
    CLIPES.mkdir(exist_ok=True)
    destino = CLIPES / f"yt_{info['id']}_{int(ini)}.mp4"
    if destino.exists():
        return destino
    # baixa só o pedaço (com uma folga de 2s de cada lado pra não cortar no meio da fala)
    r = subprocess.run([*YTDLP,"-q", "--no-warnings",
                        "--ffmpeg-location", FFMPEG,
                        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                        "--merge-output-format", "mp4",
                        "--download-sections", f"*{max(0, ini - 2):.0f}-{fim + 2:.0f}",
                        "--force-keyframes-at-cuts", "-o", str(destino),
                        f"https://www.youtube.com/watch?v={info['id']}"],
                       capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0 or not destino.exists():
        print(f"   [não consegui baixar: {(r.stderr or '').strip()[-200:]}]")
        return None
    return destino


def modo_youtube(cfg, so_listar=False):
    if "youtube" not in cfg:
        print('Falta o bloco "youtube" no config.json.')
        return
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print("Falta o yt-dlp. Rode:  python -m pip install yt-dlp")
        return
    yt = cfg["youtube"]
    if not yt.get("ativar", True):
        print("YouTube desligado no config (youtube.ativar = false).")
        return
    print("Procurando os vídeos de GTA mais vistos no YouTube...")
    videos = buscar_youtube(cfg)
    if not videos:
        print("Nenhum vídeo novo com momento viral. Aumente 'dias' ou diminua o mínimo de views.")
        return
    base = json.loads(json.dumps(cfg))
    base["recorte_laterais"] = yt.get("recorte_laterais", 1.0)
    base.setdefault("narracao", {}).update(yt.get("narracao", {"falar_gancho_no_inicio": False}))
    base["hashtags"] = yt.get("hashtags", cfg["hashtags"])
    tg = telegram_conectar(avisar=False) if not so_listar else None
    for info in videos:
        canal = info.get("channel") or info.get("uploader") or "?"
        handle = info.get("uploader_id") or ""
        canal_url = info.get("channel_url") or info.get("uploader_url") or ""
        selo = "" if info["_autorizado"] else "  [sem autorização confirmada]"
        print(f"\n>> {canal} | {info.get('view_count', 0)} views | \"{info.get('title', '')}\"{selo}")
        for ini, fim, forca in info["_cortes"]:
            link = f"https://youtu.be/{info['id']}?t={int(ini)}"
            print(f"   momento viral {hms(ini)} -> {hms(fim)} (força {forca:.2f})  {link}")
            if so_listar:
                continue
            video = baixar_trecho(info, ini, fim)
            if not video:
                continue
            cfg_c = dict(base)
            cfg_c["titulos"] = [t.replace("{streamer}", canal)
                                for t in yt.get("titulos", cfg["twitch"].get("titulos", cfg["titulos"]))]
            tag = re.sub(r"\W", "", handle.lstrip("@").lower())
            cfg_c["hashtags"] = base["hashtags"] + ([f"#{tag}"] if tag else [])
            # o trecho baixado já começa ~2s antes do momento
            folga = min(2.0, ini)
            finalizar_corte(
                cfg_c, video, folga, folga + (fim - ini),
                limpar_titulo(info.get("title", "")) if yt.get("usar_titulo_do_video", True) else "",
                SAIDA / f"youtube_{tag or 'canal'}_{info['id']}_{int(ini)}.mp4",
                credito=f"VÍDEO: youtube.com/{handle}" if handle.startswith("@") else f"VÍDEO: {canal}",
                creditos=(f"🎥 Créditos: {canal} - {canal_url}\nVídeo original: {link}"),
                info={"plataforma": "youtube", "streamer": canal, "login": handle,
                      "canal_url": canal_url, "views": info.get("view_count", 0), "url": link,
                      "titulo_original": info.get("title", ""), "autorizado": info["_autorizado"],
                      "momento": [ini, fim]},
                usados=(USADOS_YOUTUBE, f"{info['id']}@{int(ini) // 30}"), tg=tg,
                legenda_tg=f"🎬 {canal} | 👁 {info.get('view_count', 0)} views | trecho {hms(ini)}\n"
                           f"\"{info.get('title', '')}\"\n{link}")


def testar_voz(cfg):
    frase = "Fala galera! Isso aqui é GTA 6 no PS5. " + random.choice(cfg["ganchos"])
    wav, d = gerar_fala(frase, cfg)
    alvo = SAIDA / "teste_voz.wav"
    alvo.write_bytes(wav.read_bytes())
    print(f"Voz: {cfg.get('narracao', {}).get('voz')} ({d:.1f}s) -> {alvo}")


if __name__ == "__main__":
    ENTRADA.mkdir(exist_ok=True)
    SAIDA.mkdir(exist_ok=True)
    cfg = carregar_config()
    if "--sem-voz" in sys.argv:
        cfg.setdefault("narracao", {})["ativar"] = False
    if "--sem-legenda" in sys.argv:
        cfg.setdefault("legenda", {})["ativar"] = False
    if cfg.get("legenda", {}).get("ativar") and not {"--listar", "--testar-voz",
                                                      "--curiosidade", "--twitch-listar",
                                                      "--youtube-listar",
                                                      "--aprovar", "--testar-telegram",
                                                      "--painel", "--telegram-respostas"} & set(sys.argv):
        # Windows: baixa o whisper.cpp; Linux/nuvem: usa o faster-whisper (pip)
        if not (sys.platform != "win32" and _tem_faster_whisper()) and not preparar_whisper(cfg):
            print("   Seguindo SEM legenda (rode de novo com internet pra baixar o whisper).")
            cfg["legenda"]["ativar"] = False
    if "--instalar-legenda" in sys.argv:
        print("Legenda pronta." if cfg.get("legenda", {}).get("ativar") else "Falhou.")
    elif "--painel" in sys.argv:
        modo_painel()
    elif "--telegram-respostas" in sys.argv:
        telegram_respostas_nuvem()
    elif "--aprovar" in sys.argv:
        modo_aprovar()
    elif "--testar-telegram" in sys.argv:
        print("Telegram ok!" if telegram_conectar() else "Telegram não conectado.")
    elif "--youtube" in sys.argv or "--youtube-listar" in sys.argv:
        modo_youtube(cfg, so_listar="--youtube-listar" in sys.argv)
    elif "--twitch" in sys.argv or "--twitch-listar" in sys.argv:
        modo_twitch(cfg, so_listar="--twitch-listar" in sys.argv)
    elif "--curiosidade" in sys.argv:
        modo_curiosidade(cfg)
    elif "--testar-voz" in sys.argv:
        testar_voz(cfg)
    elif "--manual" in sys.argv:
        modo_manual(cfg)
    else:
        modo_auto(cfg, so_listar="--listar" in sys.argv)
    print(f"\nPronto. Vídeos em: {SAIDA}")
