# AI Desk Bot

Bot de trading de criptomoedas com uma **mesa de IA**, só em **paper trading** (dinheiro fictício).

- Um **núcleo quantitativo testado** (breakout com volume, filtro de tendência e stops por ATR) propõe os trades.
- Uma **mesa de agentes de IA** (News, Sentimento, Cético e Chefe) discute cada trade e só pode **vetar ou reduzir** o tamanho.
- As ordens vão para o **AI-Trader** a correr no teu PC (conta virtual de $100K) ou para um ledger local.

As ideias vêm dos dois vídeos, do guia que deste e do post do X sobre a mesa de 5 agentes; está tudo em [PESQUISA.md](PESQUISA.md).

> Nada disto é aconselhamento financeiro. Um backtest bom não garante lucros. O projeto não tem, de propósito, nenhuma ligação a dinheiro real.

## Como funciona

```
 velas diárias (Binance, grátis)
        │
   ┌────▼─────┐   breakout acima do máximo de 20/55 dias, volume ≥ 1,5x,
   │  Scout   │   moeda acima da EMA50, BTC acima da EMA200
   └────┬─────┘
   ┌────▼─────┐   as mesmas regras como camadas fixas, por ordem:
   │ Auditor  │   a primeira vermelha corta a moeda e diz o número que falhou
   └────┬─────┘
   ┌────▼─────┐   entrada, stop a 2 ATR, objetivo 3R ou trailing a 3,5 ATR,
   │  Charts  │   R:R até à resistência e a nota 0-100 do Analyst
   └────┬─────┘
   regras duras ── falha? → PASA (nenhum agente consegue anular)
        │
   ┌────▼─────┐ ┌────────────┐
   │   News   │ │ Sentimento │   em paralelo: manchetes + red flags | Fear&Greed, funding, OI
   └────┬─────┘ └─────┬──────┘
        └──────┬──────┘
          ┌────▼─────┐
          │  Cético  │   ataca o trade com números
          └────┬─────┘
          ┌────▼─────┐
          │  Chefe   │   COMPRA / ESPERA / PASA, tamanho ×0,25 a ×1,0
          └────┬─────┘
   Executor: 1% de risco por trade, máx. 4 posições por sleeve, não compra se o preço
             já fugiu > 5% desde o sinal, kill switch a -25%
        │
   AI-Trader (paper)  ·  tudo registado em journal.db
        │
   Risk: stops verificados a cada 15 min (nunca descem), trailing no fecho diário,
         sai por tempo se ao fim de 15 dias a posição não passou +0,5R
```

**4 sleeves** dividem a conta em partes iguais: `rapido-alvo` (breakout de 20 dias, alvo 3R), `rapido-trailing` (20 dias, trailing stop), `lento-alvo` (55 dias, alvo 3R) e `lento-trailing` (55 dias, trailing). Foram escolhidas pela diversidade, não pelo resultado do backtest.

## Resultados do backtest

10 moedas, velas diárias, de 2019-01 a 2026-11, com taxas de 0,1% e slippage de 0,05% por lado. Relatório completo: [reports/backtest.html](reports/backtest.html).

| | CAGR | Queda máxima | Sharpe |
|---|---|---|---|
| **Bot, fora da amostra (2021-07 em diante)** | **+12,6%** | **-20,2%** | **0,87** |
| BTC comprar e manter, mesmo período | +19,0% | -76,6% | 0,59 |
| Cesto das 10 moedas, mesmo período | +8,0% | -81,9% | 0,46 |
| Regras do guia .md (stop 4% / alvo 8%) | +0,9% | -28,8% | 0,14 |
| Bot, 2024 em diante | +7,6% | -13,6% | 0,56 |
| Bot antes da saída por tempo (fora da amostra) | +11,4% | -22,6% | 0,79 |

O que estes números dizem:
- O bot **não bate o BTC em retorno bruto** nos anos de euforia (2023-2024). A vantagem dele é cortar as quedas: em 2022 o BTC fez -64% e o bot -2,4%.
- Com mais risco por trade sobe o retorno e sobe a queda máxima. Com 1,5% por trade, o backtest deu +18%/ano com uma queda de -28% desde 2021-07 (tabela completa no relatório).
- A saída por tempo ("dinheiro parado", ideia do post do X) foi a única regra nova que melhorou os três períodos testados. A melhoria é pequena. As outras regras do post estão no bot, mas desligadas (ver [PESQUISA.md](PESQUISA.md)).
- A camada de IA **não está no backtest**, porque um LLM já conhece o que aconteceu nesses anos. O efeito dela mede-se em paper trading com `review`.

## Instalação (já feita neste PC)

```bash
cd C:\Users\jose\BOT\ai-desk-bot
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

O `.env` já tem a conta de teste do AI-Trader local. Para o Telegram, preenche `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no `.env`. Para usar a API da Anthropic em vez do teu login do Claude Code, preenche `ANTHROPIC_API_KEY` e muda `backend = "anthropic"` no `config.toml`.

## Painel

Abre **http://127.0.0.1:8899** no browser. Só funciona neste PC e não envia ordens. Pode pausar as compras novas (o Risk continua a guardar as posições) e mudar parâmetros no `config.toml`.

| Secção | O que mostra |
|---|---|
| Visão geral | Capital, cash, queda desde o pico, estado do servidor e das tarefas, kill switch, o que cada agente está a fazer agora, o interruptor para pausar compras novas, posições abertas (com o estado do Risk) |
| Radar do Scout | O relatório do Auditor (em que camada foi cortada cada moeda, hoje e nos últimos dias), quão perto cada moeda está do máximo de 20 e 55 dias, e para cada moeda: as camadas do Auditor por ordem, a nota do Analyst e o que o Executor e o Risk fariam se comprasse agora |
| Mesa de IA | O que cada um dos 9 agentes faz, as reuniões com a conversa completa e as fontes, e um botão para simular uma reunião sobre qualquer moeda (~$0,25, sem ordens) |
| Estratégias | As 4 sleeves explicadas em português, o backtest de cada uma e a comparação com o BTC |
| Parâmetros | Os números de cada agente com limites. Mudas, testas no backtest (atual vs proposto, em dois períodos) e só depois guardas no `config.toml`. Fica registo de cada alteração |
| Atividade | As ordens enviadas para o AI-Trader e o registo do bot, com filtro |
| Ferramentas | Os outros repositórios instalados, o que fazem e o comando para os usar |

Para o abrir à mão: `.venv\Scripts\python.exe -m deskbot dashboard`. Para arrancar sozinho quando entras no Windows, corre uma vez `powershell -ExecutionPolicy Bypass -File .\agendar_painel.ps1` (cria a tarefa `AI Desk Dashboard`).

## Telemóvel

Há uma versão do painel feita para o telemóvel em **/m**: o saldo em grande, o gráfico com as compras e vendas marcadas, os agentes em cartões, a atividade, um sino com a última notícia de cada agente e um interruptor para pausar compras. Pode ficar no ecrã principal como uma app.

Neste PC abre em http://127.0.0.1:8899/m. Para a abrir no telemóvel (no mesmo Wi-Fi que o PC):

1. No `config.toml`, em `[dashboard]`, põe `lan = true`.
2. Reinicia o painel no PowerShell:
   `Stop-ScheduledTask -TaskName "AI Desk Dashboard"; Start-ScheduledTask -TaskName "AI Desk Dashboard"`
3. O Wi-Fi de casa tem de estar como rede **Privada** (Definições → Rede e Internet → Wi-Fi → a tua rede → Tipo de perfil de rede → Privada).
4. Deixa a porta 8899 passar a firewall só em redes privadas. Num PowerShell **como administrador**:
   `New-NetFirewallRule -DisplayName "AI Desk Bot - telemovel" -Direction Inbound -Protocol TCP -LocalPort 8899 -Profile Private -Action Allow`
5. No painel do PC carrega em **Telemóvel** e lê o QR code com a câmara do telemóvel.
6. Para ficar como app: no iPhone, Safari → Partilhar → "Adicionar ao ecrã principal"; no Android, Chrome → ⋮ → "Adicionar ao ecrã principal".
7. No iPhone, a app do ecrã principal não vê a chave que o Safari guardou, por isso pede uma **password**. Define-a no painel do PC (botão **Telemóvel** → "Password para a app do ecrã principal") ou no Safari já com acesso (**Conta** → **Password da app**). Depois abre a app e entra com ela. Só se guarda um hash da password (`phone_password.json`, fora do git), e 5 tentativas erradas bloqueiam esse aparelho 5 minutos.

Segurança:
- O link do QR tem uma chave secreta (guardada em `phone_token.txt`). Sem ela, o telemóvel não vê dados. Com ela, pode ver tudo, pausar compras e mudar parâmetros. Não a partilhes.
- Se perderes o telemóvel, carrega em "Gerar link novo" no painel do PC: o link antigo deixa de funcionar.
- A ligação é http simples dentro da rede de casa. Não abras a porta 8899 no router. Para veres o painel fora de casa, usa uma VPN privada como o Tailscale no PC e no telemóvel.
- Para desligar: `lan = false` e reinicia o painel. Para tirar a regra da firewall: `Remove-NetFirewallRule -DisplayName "AI Desk Bot - telemovel"` (como administrador).

## App das 2 mesas (telemóvel)

A app mostra agora só duas mesas: **Futuros** (perpétuos da Hyperliquid, abre na INVO) e **Memecoins** (Solana, abre na FOMO). As duas correm em sessões que arrancas na app, sempre em papel. O texto abaixo descreve as mesas contínuas antigas (futuros da Binance e ações), que já não aparecem na app.

A app do telemóvel abre agora em **/app**, num visual claro: um ecrã de abertura e depois as mesas, cada uma com as suas regras e o seu saldo fictício de 1 000 €.

| Mesa | O que negoceia | Gestão | Backtest |
|---|---|---|---|
| Memecoins | Moedas novas e em alta na Solana, em sessões de 30 min, 2 h ou 12 h que arrancas na app | Stop -6%, trailing a partir de +20%, 8% do saldo por moeda, no máximo 5 | Não há (não existe histórico grátis ao minuto) |
| Futuros | Perpétuos de BTC, ETH, SOL, BNB e XRP, long e short, velas de 4 h | 1% de risco por trade, stop a 2,5 ATR, trailing a 3 ATR, alavancagem máxima 2x, funding pago ou recebido | Desde 2020: +29,6%/ano, queda máx. -31% (BTC: +42%/ano, -77%) |
| Ações | S&P 500 (SPY) quando está acima da média de 200 dias; senão ouro ou obrigações | Revisto uma vez por mês, sem alavancagem, sem stop fixo | Desde 2008: +9,6%/ano, queda máx. -26% (SPY: +11,2%/ano, -52%) |

Cada mesa mostra: a faixa de cotações, o saldo, o gráfico, as posições, os trades fechados, o registo em direto do que cada agente decidiu e a gestão (regras, backtest e controlos: pausar a mesa, ou arrancar e parar uma sessão de memecoins). O bot principal continua em /m.

Futuros e Ações correm no mesmo ciclo de 15 minutos do bot (`python -m deskbot run`); para correr só elas: `python -m deskbot desks`. Os dados vêm das APIs públicas da Binance (futuros) e do Yahoo Finance (ações). Os estados ficam em `desks/`.

Os backtests não são promessas: as moedas e os ETFs foram escolhidos hoje, e a regra das ações foi escolhida entre 5 variantes conhecidas.

## Modo memes (5 agentes, como no vídeo "gmgn mode")

A mesa de Memecoins abre agora em **/memes**, num ecrã escuro:
1. **Arranque** "git clone · 5 repos", com os 5 repositórios do vídeo e as estrelas de cada um.
2. **Ligar**: escolhes a duração (5 min a 12 h) e o saldo fictício (50, 110, 500 ou 1000 €).
3. **Ecrã principal**: saldo, gráfico, os últimos BUY/SELL, a tabela "agentes · a trabalhar juntos", o diagrama "5 agentes → núcleo" e avisos no topo quando há compras, vendas ou baleias.
4. **Resumo** no fim: quanto pôs, quanto vale, trades, melhor e pior moeda, acerto.

| Agente | Inspirado em | O que faz aqui |
|---|---|---|
| TRENDS | 8bitsats/Solana-MCP-Trading-Server | Pools em alta e moedas que saíram do pump.fun (GeckoTerminal) |
| SMARTS | GMGNAI/gmgn-skills | Baleias: compras de $500 ou mais nos últimos 30 min; precisa de 3 baleias e de mais compras do que vendas |
| DEBATE | TauricResearch/TradingAgents | 5 votos bull/bear e a equipa de risco (mint e freeze, top 10, honeypot, vendas) |
| ROUTER | sendaifun/solana-agent-kit | Cotação real da Jupiter para o tamanho do trade; recusa mais de 2% de impacto |
| EXEC | 0xsyncroot/opentrade | Compra e vende em papel ao preço da Jupiter; preço a cada 5 s; stop −8% e trailing |

O núcleo só compra quando os 5 concordam. Cada moeda tem links para a FOMO, o pump.fun e o DexScreener, só para ver.

**O que é diferente do vídeo, de propósito:**
- É tudo em papel. O vídeo liga uma carteira à GMGN e negoceia dinheiro real; aqui não há carteira, chaves nem ordens.
- Não usa a API da GMGN: precisa de uma chave pessoal criada em gmgn.ai/ai, e a chave pública de demonstração deles é só para testes. As baleias vêm do GeckoTerminal.
- A FOMO não tem API pública, por isso não há ligação a ela.
- O vídeo mostra $110 a passar a $3 498 em 12 h, com 84% de acerto. É publicidade, não um resultado verificável.

Para correr sem a app: `python -m deskbot memedesk --minutes 30 --eur 110`.

## Teste de memecoins (papel)

`python -m deskbot memetest --minutes 30 --eur 1000` corre durante 30 minutos uma mesa de memecoins da Solana parecida com a do post do X. Só usa dinheiro fictício: não há carteira, chaves nem ordens. É separada do bot principal e não toca na conta do AI-Trader.

| Agente | O que faz |
|---|---|
| Scanner | Vê os pools novos da PumpSwap (moedas que saíram do pump.fun) e os pools em alta na Solana (GeckoTerminal). Corta liquidez < $40 000, subida > 80% numa hora, pool com menos de 10 minutos (espera até 60) |
| Auditor | Por ordem, a primeira vermelha corta: mint revogada, freeze revogado (rede Solana), top 10 de holders < 25%, não é honeypot, houve vendas na última hora, liquidez ≥ $40 000 |
| Analyst | 4 números de 0 a 1 (vivo, orgânico, flush, perseguição) e uma nota; precisa de vivo ≥ 0,70, orgânico ≥ 0,65, perseguição ≤ 0,30 e nota ≥ 70 |
| Executor | 8% do saldo, no máximo 2% da liquidez do pool e 5 posições; não compra se o preço fugiu > 15% ou a liquidez caiu > 20% |
| Risk | Preço a cada 15 s (DexScreener): stop -6%, trailing a partir de +20% que vende 12% abaixo do máximo, sai se a liquidez desaparecer |

Cada compra e venda paga 1% de custos mais o impacto no pool. No fim, o que está aberto é vendido ao último preço. Tudo fica em `memetest/`: os eventos de cada agente, o estado (que o telemóvel mostra num cartão no Início) e `relatorio.md`.

O que falta em relação ao post: não verifica a carteira do criador, os bundles, o LP queimado nem as delegações, porque isso precisa de um serviço pago ou de uma API privada. Um teste de 30 minutos não diz se a estratégia ganha dinheiro.

## Comandos

Corre-os na pasta `ai-desk-bot`, com `.venv\Scripts\python.exe -m deskbot <comando>`:

| Comando | O que faz |
|---|---|
| `backtest` | Testa as 4 sleeves, o walk-forward e os benchmarks, e escreve `reports/backtest.html` |
| `scan` | Mostra os sinais de hoje, sem IA e sem ordens |
| `desk BTC` | Demonstração: a mesa de IA discute uma moeda, sem ordens |
| `run` | Um ciclo: verifica os stops e, se houver vela diária nova, corre a mesa |
| `run --loop` | Fica a correr 24/7 (stops a cada 15 min, mesa às 00:10 UTC) |
| `status` | Conta, posições abertas, stops e últimas ordens |
| `review` | O que aconteceu depois de cada decisão da mesa: os vetos da IA ajudaram? |

## Pôr a correr 24/7

Já está configurado no **Agendador de Tarefas do Windows**, sem janelas abertas:

| Tarefa | Quando corre | O que faz |
|---|---|---|
| `AI-Trader Server` | Quando entras no Windows (e reinicia se cair) | Servidor de paper trading em http://localhost:8000, através de `launchers\aitrader_server.py` |
| `AI Desk Bot` | A cada 15 minutos | Um ciclo de `run`: verifica os stops e, se houver vela diária nova, reúne a mesa |
| `AI Desk Dashboard` (opcional, com `agendar_painel.ps1`) | Quando entras no Windows | O painel em http://127.0.0.1:8899 |

- O que o bot faz fica em `logs\bot.log` (uma linha "Cycle done" por ciclo).
- Os erros do servidor ficam em `..\AI-Trader\service\server\logs\console.log`.
- O PC tem de estar ligado e sem suspender. Para não dependeres dele, usa um VPS barato (os dois vídeos dizem o mesmo).

Para gerir as tarefas no PowerShell:
- **Pausar o bot:** `Disable-ScheduledTask -TaskName "AI Desk Bot"`
- **Retomar o bot:** `Enable-ScheduledTask -TaskName "AI Desk Bot"`
- **Remover tudo:** `Unregister-ScheduledTask -TaskName "AI Desk Bot","AI-Trader Server","AI Desk Dashboard" -Confirm:$false`

Os ficheiros `start_aitrader.bat` e `start_bot.bat` servem para arrancar à mão com janela, se removeres as tarefas. Não os uses com as tarefas ativas, ou ficas com dois servidores a disputar a porta 8000.

## Custos

- Dados de mercado, notícias (RSS) e sentimento: grátis.
- IA: cada discussão faz 4 chamadas ao Claude (cerca de 80 segundos). Com `backend = "claude-cli"` usa a tua subscrição do Claude Code; o custo de tabela estimado é de ~$0,25 por discussão. Há sinais em poucos dias por mês.

## Antes de pensar em dinheiro real

1. Corre em paper pelo menos **3 meses**.
2. Compara o `status` com o backtest: o número de trades, o R médio e a queda máxima devem andar perto.
3. Usa `review` para ver se os vetos da IA evitaram perdas ou só cortaram ganhos. Se só cortaram ganhos, usa `backend = "none"`.
4. Este projeto não liga a nenhuma corretora real. Essa decisão, e o risco que vem com ela, é tua.

## Ficheiros

```
deskbot/
  strategy.py   núcleo quantitativo (sinais, plano, stops); o mesmo código no backtest e no live
  backtest.py   motor de backtest, métricas, walk-forward
  auditor.py    as regras de entrada como camadas por ordem (a primeira vermelha corta)
  desk.py       a mesa de IA e as regras duras
  settings_edit.py  parâmetros editáveis pelo painel, com limites
  futures.py    mesa de Futuros (perpétuos, long e short) e o seu backtest
  stocks.py     mesa de Ações (S&P 500 com filtro de tendência) e o seu backtest
  paperdesk.py  livro-razão em papel partilhado pelas mesas
  desks_api.py  o que a app vê de cada mesa, sessões de memecoins, pausas
  dashboard_static/app.html  a app das 2 mesas (/app)
  memedesk.py   modo memes: 5 agentes (TRENDS, SMARTS, DEBATE, ROUTER, EXEC) e o núcleo
  dashboard_static/memes.html  o ecrã escuro do modo memes (/memes)
  memetest.py   teste em papel de uma mesa de memecoins da Solana (python -m deskbot memetest)
  phone.py      acesso do telemóvel: chave secreta, endereço na rede, QR code, ícone
  dashboard_static/mobile.html  a versão do painel para o telemóvel (/m)
  llm.py        Claude via claude CLI ou Anthropic SDK
  context.py    notícias RSS, Fear & Greed, funding/open interest
  live.py       ciclo live (stops, mesa diária, ordens)
  brokers.py    AI-Trader local e ledger de papel
  journal.py    diário SQLite com o raciocínio de cada decisão
config.toml     todos os parâmetros
tests/          testes (incluindo um que garante que os sinais não usam dados do futuro)
```
