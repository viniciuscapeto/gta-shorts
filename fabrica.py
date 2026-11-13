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
SR = 8000          # taxa de amostragem usada só para análise
JANELA = 0.5       # segundos por bloco de análise


def carregar_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


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


def transcrever(video: Path, ini: float, dur: float, cfg: dict):
    """Retorna lista de (inicio, fim, texto) das falas do jogo dentro do corte."""
    leg = cfg.get("legenda", {})
    exe = achar_whisper_exe()
    if not exe:
        return []
    TMP.mkdir(exist_ok=True)
    wav, saida = TMP / "fala_jogo.wav", TMP / "fala_jogo"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{ini:.2f}", "-t", f"{dur:.2f}",
                    "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
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
    idioma = dados.get("result", {}).get("language", "")
    para = leg.get("traduzir_para", "")
    falas = []
    for s in dados.get("transcription", []):
        texto = re.sub(r"\[.*?\]|\(.*?\)|♪", "", s.get("text", "")).strip()
        if len(texto) < 2 or ALUCINACOES.match(texto):
            continue
        a, b = s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000
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
               fala_inicio: str = "", roteiro=None) -> list:
    """Renderiza o corte. Retorna (textos narrados, legendas das falas do jogo).
    roteiro = (wav, dur, palavras, texto): narração contínua (modo curiosidade)."""
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
        if audio_jogo and cfg.get("legenda", {}).get("ativar", False) and achar_whisper_exe():
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

    filtro = (
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=25:2,eq=brightness=-0.18[fundo];"
        f"[b]crop=iw*{zoom}:ih,scale=1080:-2[jogo];"
        "[fundo][jogo]overlay=(W-w)/2:(H-h)/2[v0];"
        f"[v0]drawtext=fontfile='{fonte}':textfile='{esc(txt)}':text_align=C:"
        "fontsize=78:fontcolor=white:borderw=7:bordercolor=black:line_spacing=12:"
        "x=(w-text_w)/2:y=520-text_h[v1];"
        f"[v1]drawtext=fontfile='{fonte}':textfile='{esc(perfil)}':"
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


def escrever_legenda(destino: Path, gancho: str, cfg: dict, textos=((), ())):
    falas, legendas = textos
    titulo = random.choice(cfg["titulos"]).replace("{gancho}", gancho)
    tags = " ".join(cfg["hashtags"])
    narrado = ("\n\nNARRAÇÃO:\n" + "\n".join(f"- {f}" for f in falas)) if falas else ""
    jogo = ("\n\nFALAS DO JOGO (legendadas):\n" + "\n".join(f"- {l}" for l in legendas)
            if legendas else "")
    destino.with_suffix(".txt").write_text(
        f"TÍTULO:\n{titulo}\n\nLEGENDA (copiar e colar):\n{titulo} {tags}{narrado}{jogo}\n",
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
                                                      "--curiosidade"} & set(sys.argv):
        if not preparar_whisper(cfg):
            print("   Seguindo SEM legenda (rode de novo com internet pra baixar o whisper).")
            cfg["legenda"]["ativar"] = False
    if "--instalar-legenda" in sys.argv:
        print("Legenda pronta." if cfg.get("legenda", {}).get("ativar") else "Falhou.")
    elif "--curiosidade" in sys.argv:
        modo_curiosidade(cfg)
    elif "--testar-voz" in sys.argv:
        testar_voz(cfg)
    elif "--manual" in sys.argv:
        modo_manual(cfg)
    else:
        modo_auto(cfg, so_listar="--listar" in sys.argv)
    print(f"\nPronto. Vídeos em: {SAIDA}")
