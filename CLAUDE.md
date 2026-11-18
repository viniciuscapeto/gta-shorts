# Três Estrelas (@tresestrelasclips) — fábrica de shorts de GTA 6

Fale com o usuário em **português do Brasil**, direto e sem enrolação. Ele não é programador:
dê passo a passo de clique, não jargão.

## O que é
Página de cortes de **GTA 6** (Instagram, TikTok, YouTube, Facebook). A fábrica pega clipes das
**lives brasileiras de GTA 6 na Twitch**, edita em 9:16 (título no topo, crédito
"CLIPE: twitch.tv/login", legenda das falas, @ no rodapé) e manda pro **Telegram** pra aprovação.

- `fabrica.py` — tudo: Twitch (`--twitch`), YouTube (`--youtube`, desligado), edição (`renderizar`),
  legenda (whisper.cpp no Windows / faster-whisper na nuvem), Telegram, Instagram API (`--postar-instagram`).
- `painel.py` + `painel/` — painel local (`painel.bat`, http://localhost:8765): aprovar, abas por rede,
  números, configurações, registro. Com Telegram configurado, baixa os aprovados da nuvem pra `saida/aprovados`.
- `.github/workflows/fabrica.yml` — **"Fábrica na nuvem"**: roda a cada ~25 min (cron `*/25`), busca, edita,
  manda pro Telegram, posta no Instagram (se houver token), resumo "🤖 Rodada da nuvem" no Telegram.
- `config.json` — tudo configurável. `segredos.json` (local, no .gitignore) guarda as chaves.
- `docs/` — site no GitHub Pages (viniciuscapeto.github.io/gta-shorts): início, privacidade, termos.
- `fila_instagram/` — **6 Reels aprovados esperando postagem**, com legenda pronta (ver `fila_instagram/LEIA.txt`).

## Regras que o usuário pediu (não mudar sem ele pedir)
- **Só GTA 6.** Nada de GTA 5, GTA RP, FiveM, "cidade", servidor/WL. Lives na categoria GTA 6 com título de RP
  são ignoradas (`bloquear_live_com`), clipes com "gta 5"/"rp" no título também (`bloquear_no_titulo`).
  Comparação por PALAVRA inteira ("gta v" não pode bloquear "GTA VI" — já foi bug).
- **Só lives brasileiras** (idioma pt), nada de gringo. Prioridade pros streamers da lista;
  alanzoka traz mais clipes (`"clipes": 8`). Paulinho é `paulinholokobr`, Bistecone é `bisteconee`.
- Crédito sempre ao streamer (na tela e na legenda). Política do site: remove em 48h se pedirem.
- Antes de postar, conferir o frame: recusar GTA 5/RP, nudez, título com letra solta ou "□".
- Ritmo de postagem combinado: **1 Reel a cada ~30 min**.
- Legenda de post: título chamativo + "⭐⭐⭐ Segue a @tresestrelasclips..." + pergunta pra comentar +
  "🎥 Créditos: Nome - twitch.tv/login" + hashtags #gta6 #gtavi #gta #vicecity #leonida #<login> ...

## Estado em 18/11/2026 (fim do dia no PC do trabalho)
- Instagram @tresestrelasclips: conta **Creator**, ~8 Reels postados, primeiras views chegando.
  Postados: Ele tá jogando oficial, Primeiro roubo, Gordo quase infartou, Babando, KKKKKKKKK (Snopey),
  Lucia é mãe, Que isso Mandy?. O **Pé Grande (01 da fila) pode ter subido** — upload travou; confirmar no perfil.
- Nuvem funcionando (Twitch + Telegram + legenda). Segredos no GitHub: TWITCH_CLIENT_ID,
  TWITCH_CLIENT_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
- Até agora a postagem foi feita **pela tela** (script clicando no Chrome do PC do trabalho). Isso acabou:
  o PC do trabalho foi limpo e a rede de lá bloqueia Twitch/YouTube/developers.facebook.com.

## Próximos passos
1. **Em casa:** baixar o repo, `painel.bat`, colar chaves no painel (Telegram: @BotFather /mybots > API Token;
   Twitch: ID no dev.twitch.tv; segredo novo exige atualizar TWITCH_CLIENT_SECRET no GitHub também).
2. **Postar a `fila_instagram/`** (01 a 06), conferindo o 01 antes.
3. **Terminar a API do Instagram** (pra nuvem postar sozinha): app "Tres Estrelas Uploader" criado em
   developers.facebook.com com um Facebook novo; travou pedindo **confirmar celular na Central de Contas**
   (accountscenter.facebook.com/personal_info/contact_points). Alternativa: usar o Facebook pessoal dele.
   Caminho: Casos de uso > Instagram > "API setup with unified login" (pede portfólio empresarial) >
   Generate token com @tresestrelasclips > salvar no GitHub como **INSTAGRAM_TOKEN** (token deve começar
   com "IG"; se vier "EAA" é token de Facebook e o código precisa de ajuste). Depois pôr
   `"postar_pela_nuvem": true` em config.json (senão o painel do PC disputa os cliques do Telegram).
   O código (`modo_postar_instagram`, upload resumable em rupload.facebook.com, API v24.0) foi testado só
   com API simulada — validar na primeira rodada real. Token vale 60 dias.
4. **TikTok:** ele quer postar à noite pelo PC dele. App em developers.tiktok.com ainda não criado
   (sem aprovação, a API só posta como "só eu").
5. YouTube Shorts: API exige auditoria (posta privado até aprovar). Não iniciado.

## Gotchas
- PowerShell 5.1: não usar `&&`; para mensagens de commit com aspas use `git commit -F arquivo`.
- Salvar arquivos com acento via ferramenta de edição (Set-Content -Encoding ascii estraga o .yml).
- `config.json` sem BOM (o Python lê com utf-8).
- O resumo do Telegram mostra "versão <sha>" — se não bater com o último commit, é rodada antiga (Re-run).
