# Pesquisa: o que tirei dos vídeos e do guia

## 1. YouTube: "3 free Github repos to print $$$ AI trading" (Miles Deutscher, 12:36)

Transcrição completa analisada. Os três repositórios do vídeo e o papel de cada um:

| Repo | Papel | O que aproveitei |
|---|---|---|
| **Freqtrade** | Motor de execução: tu dás as regras, ele executa. Backtest, dry-run, controlo por Telegram | Separar **estratégia testada** de **execução**. Ordem obrigatória: backtest → dry-run fora da amostra → paper → só depois dinheiro real |
| **Vibe-Trading** | Investigação: transforma ideias em estratégias testadas; tem uma "alpha zoo" com 450+ alphas | A melhor alpha que encontrou no vídeo tinha "edge pequeno face a comprar e manter". Comparar sempre com comprar e manter |
| **NoFx** | Terminal de IA com autopilot na Hyperliquid, mas só em modo live | Os motores prontos já trazem o "loop" pronto: ler o mercado → decidir → executar → **registar o raciocínio** |

Avisos do próprio autor que entraram no desenho do bot:
- "O backtest pode mostrar centenas ou milhares de % de lucro. O teste real é como se porta ao vivo."
- O bot tem de correr 24/7: computador sempre ligado ou um VPS.
- Ele próprio diz que as estratégias que mostra são só exemplos, não recomendações.

## 2. Vídeo do TikTok (@antonspain_ia, 48 s, espanhol)

Transcrição com Whisper e análise de 16 frames. Mostra uma "mesa de trading" de 6 bots no Grok, num chat de grupo:

| Bot | Função no vídeo | No AI Desk Bot |
|---|---|---|
| Jefe | Dirige a mesa | **Chefe** (IA): decisão final COMPRA / ESPERA / PASA |
| Scout | Procura volume anómalo (ex: "BTC volume 2,4x") | **Scout** (código): breakout com volume ≥ 1,5x a média |
| News | Explica porque se move, com link e hora de cada fonte | **News** (IA): manchetes de CoinDesk, Cointelegraph e Decrypt, red flags e fontes |
| Sentimiento | "Lê as pessoas" | **Sentimento** (IA): Fear & Greed, funding, open interest |
| Charts | Entrada, stop e objetivo a partir das velas diárias | **Charts** (código): entrada, stop por ATR, objetivo, R:R |
| Escéptico | Contraria cada trade. No vídeo veta um long em BTC porque o R:R era só 1,4:1 | **Cético** (IA) + regra dura: R:R mínimo de 2:1 |

**Alerta:** os números do vídeo ("$87 → $8.391 em 18 horas", "$90 → $14.220 numa semana") são capturas de ecrã sem verificação e não são realistas. O vídeo termina a pedir "comenta GROK" para receber o guia, ou seja, é marketing. A ideia da mesa com um cético é boa; os números não contam.

## 3. Guia `ai-trading-repos-setup_2.md` (8 repositórios)

- **TradingAgents** (tier S): 12 agentes, com debate bull vs bear, equipa de risco e gestor. Inspirou a estrutura Cético + Chefe.
- **LLM-Trading-Lab** (tier C): o ChatGPT geriu $100 reais durante 6 meses e acabou em $84,48 (-15%, com uma queda de 50% a partir do pico). Lição: **um LLM sozinho a decidir tudo perde dinheiro**. Por isso, no AI Desk Bot a IA só pode vetar ou reduzir trades, nunca criá-los nem aumentá-los.
- **Regras de exemplo do Freqtrade no guia** (breakout de 20 dias, RSI 50-70, stop 4%, alvo 8%): testei-as nas mesmas 10 moedas, com taxas. Fora da amostra deram **+0,7%/ano, com queda máxima de -28,8%**. Não servem.

## 4. AI-Trader (HKUDS), instalado em `C:\Users\jose\BOT\AI-Trader`

Plataforma "agent-native": os agentes registam-se, publicam sinais e fazem paper trading com $100K. Corre localmente (FastAPI + SQLite). O bot usa-o como **corretora de papel**: `POST /api/signals/realtime` com `executed_at: "now"`, e o servidor dá o preço e cobra uma taxa de 0,1%.

## 5. Conclusões que definiram o bot

1. **O núcleo tem de ser testável e testado.** Um LLM não se consegue testar no passado (já conhece o que aconteceu), por isso a decisão "há trade?" vem de regras quantitativas.
2. **A IA serve de filtro**, como o Cético do TikTok e o debate do TradingAgents: corta trades com red flags concretas e não pode aumentar o risco.
3. **Não otimizar demais.** Reotimizar os parâmetros a cada 6 meses rendeu 8,5%/ano fora da amostra, contra 12,6%/ano de 4 conjuntos fixos a correr em paralelo (números com a saída por tempo da secção 6; antes dela eram 4,4% contra 11,4%).
4. **Paper primeiro, durante meses**, com o diário a medir se os vetos da IA ajudam (`python -m deskbot review`).

## 6. Post no X: @savipww, "5 agents, 1 app, 1 prompt" (1 de novembro de 2026)

**O que é.** Um guia para pôr o Grok a construir uma mesa de 5 agentes que caça memecoins novas na Solana (as que "graduam" do pump.fun), mais uma app de iPhone e um bot de Telegram. Os agentes são o Scanner (filtra as moedas novas), o Auditor (8 verificações de segurança por ordem, a primeira vermelha mata a moeda), o Analyst (4 perguntas fechadas com uma nota de 0 a 100), o Executor (tamanho: 8% da banca, no máximo 2% da liquidez do pool) e o Risk (stop -6%, trailing de 12% a partir de +20%, sai ao fim de 3 h se não subiu 5%).

**Cuidados antes de copiar:**
- O título fala em "six figure", mas o próprio autor diz que as últimas 3 noites tiveram 7, 8 e 7 trades perdedores, e pede para copiar "a forma, não os números".
- Todos os links levam o código de referência do autor e o prompt obriga o bot a usá-los (os testes falham sem ele). Ou seja, o autor ganha com as taxas de quem segue o guia.
- O bot de copy trading usa uma API não oficial com um token de login com acesso total à conta.
- Memecoins de minutos não se conseguem testar com os dados que o bot tem. Por isso o scanner da Solana, o FOMO e o copy trading **não entraram** no bot.

**O que adaptei ao bot (a forma, não os números):**
- **Auditor:** as regras de entrada do Scout passam a ser camadas fixas, por ordem. A primeira vermelha corta a moeda, as seguintes ficam por verificar e o painel mostra o número que falhou. O bot regista os cortes de cada dia e o painel mostra que camada cortou mais.
- **Analyst:** 4 perguntas respondidas por código a partir das velas (a tendência está viva, o volume é real, há espaço até à resistência, a entrada persegue uma vela que já correu). A nota de 0 a 100 é a média.
- **Executor:** não compra se o preço já subiu mais de 5% entre o fecho do sinal e a compra.
- **Risk:** saída por "dinheiro parado" e trailing que só arranca depois de +X R. Um stop nunca desce.
- **Painel:** a pílula PAPER, os cartões do que cada agente está a fazer, o interruptor para pausar as compras novas, o separador Parâmetros (limites, teste no backtest, registo das alterações) e a linha "sem ligação" quando o painel perde o bot.

**Backtest de cada regra** (as 4 sleeves juntas, com taxas; Sharpe por período):

| Regra | Desde 2021-07: retorno/ano, queda máx., Sharpe | 2021-07 a 2023 | 2024 em diante | 2019 a 2021-07 | Decisão |
|---|---|---|---|---|---|
| Nenhuma (antes) | +11,4%, -22,6%, 0,79 | 1,09 | 0,49 | 1,97 | |
| Sai ao fim de 15 dias se < +0,5R | **+12,6%, -20,2%, 0,87** | **1,18** | **0,56** | **2,01** | **ligada** |
| Sai ao fim de 10 dias se < +0,5R | +12,2%, -19,7%, 0,87 | 1,28 | 0,48 | 2,03 | |
| Sai ao fim de 20 dias se < +0,5R | +11,0%, -22,8%, 0,78 | 1,06 | 0,49 | 1,98 | |
| Trailing só depois de +1R | +11,4%, -20,7%, 0,80 | 1,16 | 0,44 | | desligada |
| Não comprar > 2 ATR acima do nível | +11,9%, -20,8%, 0,84 | 1,21 | 0,48 | | desligada |
| Não comprar > 1 ATR acima do nível | +9,5%, -17,1%, 0,73 | 1,16 | 0,29 | | desligada |
| Nota do Analyst ≥ 50 / 60 / 70 | Sharpe 0,75 / 0,91 / 0,62 | | | | desligada |
| Preço fugiu > 5% antes da compra | igual ao "antes" | | | | ligada (só protege o live) |

O que estes números dizem:
- A saída por tempo é a única regra que melhorou todos os períodos, incluindo os anos antes de 2021-07. O ganho é pequeno (+1,2 pontos por ano, menos 2,4 pontos de queda máxima). Escolhi 15 dias depois de ver os resultados, o que puxa o número um pouco para cima, mas 10, 12 e 15 dias melhoram todos a queda máxima.
- A nota do Analyst com filtro a 60 parece boa, mas a 50 e a 70 fica pior. Isso é ruído, não uma regra, por isso a nota fica só informativa.
- O filtro "preço fugiu" não muda o backtest, porque em cripto a vela diária abre no fecho da anterior. Serve para o live: se o PC esteve desligado e o bot só corre horas depois, não compra uma moeda que já disparou.
- Todas as regras desligadas podem ser ligadas no separador Parâmetros do painel, que as testa no backtest antes de as guardar.
