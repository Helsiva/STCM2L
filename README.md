# stcm2l-tool

Kit de tradução para arquivos de script **STCM2L** — a engine usada nas Visual Novels
da **Otomate / Rejet** (PlayStation Vita, PSP e ports de PC).

Extrai o texto dos `.DAT`/`.BIN`, permite traduzir (manual ou via DeepL/Google) e
injeta de volta **recalculando todos os ponteiros do arquivo**, então o texto em
PT-BR pode ser maior ou menor que o japonês original sem quebrar o jogo.

- Python **3.10+**, **sem dependências obrigatórias** (só `struct`, `json`, `pathlib`,
  `argparse`, `urllib` da biblioteca padrão).
- Processamento individual ou em lote (uma pasta inteira de uma vez).
- Round-trip verificável: `verify` lê e reescreve o arquivo e compara **byte a byte**.

---

## Instalação

Não há instalação. Baixe o repositório e rode o `stcm2l.py`:

```powershell
git clone https://github.com/Helsiva/STCM2L.git
cd STCM2L
python stcm2l.py selftest
```

O `selftest` gera arquivos STCM2L sintéticos (com ponteiros reais) e valida o ciclo
completo: parse → rebuild idêntico → extrair → traduzir → injetar → conferir ponteiros.
Se ele passar, o ambiente está pronto.

> Opcional: `pip install googletrans==4.0.0rc1` — só se for usar `--provider googletrans`.
> DeepL e Google Cloud Translation funcionam apenas com a biblioteca padrão.

---

## Fluxo de trabalho

### 0. Confira se o parser entende os seus arquivos (faça SEMPRE isto primeiro)

```powershell
python stcm2l.py verify .\scripts
```

`verify` lê cada arquivo, reserializa do zero e compara com o original. Qualquer
arquivo marcado como `DIFERE` **não deve ser injetado** — significa que há alguma
variação de cabeçalho que o parser ainda não reproduz fielmente.

```powershell
python stcm2l.py info .\scripts\EV_0001.DAT     # cabeçalho, opcodes, encoding detectado
```

### 1. Extrair

```powershell
# um arquivo
python stcm2l.py extract .\scripts\EV_0001.DAT -o .\txt\EV_0001.json

# pasta inteira (lote)
python stcm2l.py extract .\scripts -o .\txt

# formato de texto simples, mais confortável para traduzir à mão
python stcm2l.py extract .\scripts -o .\txt --format txt
```

#### Deu "0 textos"?

`verify` passar e `extract` devolver zero significa que o arquivo foi entendido,
mas nenhum bloco de dado pareceu diálogo. O comando `diag` diz por quê:

```powershell
python stcm2l.py diag .\scripts\600_sub_NO00.DAT
```

Ele imprime três coisas:

1. **todos os blocos de dado**, com o motivo exato da recusa (byte de controle,
   codificação errada) e, quando existe, a codificação alternativa que funciona;
2. **uma varredura crua** de strings cp932/UTF-16 no arquivo inteiro, ignorando a
   estrutura, dizendo em que região cada string caiu (`PAYLOAD` de um bloco,
   trecho cru, tabela de exports…);
3. **um veredito**: o texto está lá e a heurística recusou, o texto está fora dos
   blocos reconhecidos, ou o arquivo realmente não tem diálogo (só chamadas de
   voz e flags — nesse caso o texto está em outro arquivo do CPK).

Escapes rápidos: `--encoding cp932` (ou `utf-16-le`) força a codificação, e
`extract --all-blocks` exporta todos os blocos, inclusive os que não parecem texto.

> **Confira a codificação detectada antes de traduzir.** `cp1252` e `latin-1`
> mapeiam byte a byte e por isso **nunca falham**: se alguns blocos do arquivo não
> forem texto, elas decodificam tudo e vencem a detecção, e aí o roteiro japonês
> inteiro sai mojibakado no `.json` (`発言者名` vira `”Œ¾ŽÒ–¼`). A detecção
> hoje só recorre a elas quando nenhuma codificação estrita (`utf-8`, `cp932`,
> `utf-16-le`) cobre pelo menos 60% dos blocos — mas se o `info` mostrar
> `cp1252` ou `latin-1` num script japonês, force com `--encoding cp932`.
> Traduzir mojibake produz lixo, e o `original` mojibakado não casa mais com o
> `.DAT` na hora de injetar.

Com `-r` numa pasta, a saída **espelha a estrutura de subpastas** da entrada.
Isso importa na hora de devolver os arquivos para dentro do container: a árvore
gerada pode ser copiada inteira por cima da extraída, sem redistribuir arquivo a
mão. Caminho com espaço precisa de aspas (`"...\game pt\script"`).

### 2. Traduzir

**Sem chave nenhuma (`gtx`, o padrão):**

```powershell
python stcm2l.py translate .\txt -o .\txt_ptbr `
    --source JA --target PT-BR --only-cjk --cache .\cache.json
```

`gtx` fala com o endpoint público `translate_a/t` usando só a biblioteca padrão — é o
mesmo motor do site do Google Tradutor. Não precisa de chave nem de dependência, e vai
**em lote**: manda até 500 falas por requisição (`--batch-size`), então um script
inteiro sai em segundos em vez de minutos. Medido: 124 falas em **2,8 s** contra 36 s
uma-a-uma; 2000 falas cabem numa única requisição, em ~5 s.

É um endpoint **não oficial**, então tem duas defesas: ao levar `429` ele tenta
sozinho as outras variantes de `client` (`dict-chrome-ex` costuma passar onde `gtx` é
barrado) e memoriza a que funcionou; se o lote for recusado por tamanho, ele se parte
ao meio e repete, sem você ter que adivinhar um `--batch-size`. Se o lote for barrado
por IP em todas as variantes, ele **avisa e cai sozinho** para o modo uma-a-uma —
que também dá para forçar direto com `--provider gtx-serial`.

A qualidade continua sendo a do Google, inferior à do DeepL em JA→PT: revise os
`.json` antes de injetar.

**Com chave (DeepL, melhor qualidade):**

```powershell
python stcm2l.py translate .\txt -o .\txt_ptbr `
    --provider deepl --api-key "SUA-CHAVE:fx" --source JA --target PT-BR `
    --cache .\cache.json
```

A conta Free do DeepL dá 500 mil caracteres/mês e reseta todo mês; quando a cota
estoura, o `gtx` acima assume sem custo nenhum.

O `--cache` é gravado **a cada lote**: se a tradução morrer no meio, rodar de novo com
o mesmo arquivo de cache retoma de onde parou, sem re-traduzir o que já saiu.

Outros provedores: `--provider google` (chave da Google Cloud Translation API),
`--provider googletrans` (biblioteca não oficial — **costuma quebrar**: ela depende de
um formato de resposta que o Google muda sem aviso, além de ser lenta; prefira `gtx`),
`--provider gtx-serial` (o `gtx` uma fala por requisição, só como plano B),
`--provider none`.

> **Não mande identificador para o tradutor.** O `extract` exporta todo bloco de texto do
> script, e isso inclui IDs de voz, nomes de arquivo e flags (`NO00_0012`,
> `bgm_theme_01.at9`, `SYSTEM_GLOBAL`). Traduzir isso quebra o jogo, que deixa de achar o
> recurso. Dois filtros, conforme o idioma do script:
>
> - `--only-cjk` — script **japonês**: traduz só o que tem kana/kanji.
> - `--skip-ids` — script **em inglês, ou misto**: traduz fala em qualquer idioma, mas
>   preserva o que tem cara de identificador. Num script em inglês, `--only-cjk` pularia tudo.
>
> **Jogo com japonês E inglês no mesmo lote é caso de `--skip-ids`.** O que conta como
> identificador ali inclui **token único sem espaço e sem pontuação de fim de frase**:
> `switch`, `flag`, `jump`, `end`, `r`. Num script em inglês essas palavras quase nunca são
> diálogo e quase sempre são palavra-chave da engine — e traduzir uma delas (`switch` →
> `trocar`) faz o jogo perder a palavra e parar de andar. Fala de uma palavra só vem
> pontuada (`Yes.`, `Ouch!`) e continua sendo traduzida; o preço do critério é uma
> eventual interjeição sem ponto que fica em inglês, que é cosmético.
>
> O que for pulado fica com `translation` vazio, e o `inject` preserva o original.

Para descobrir o idioma de cada arquivo antes de traduzir, use o `info`: a linha
`conteudo` separa japonês, prosa latina e identificadores, e mostra se o texto está
embutido nas ações ou num pool solto.

**Manual:** edite o `.json` (campo `"translation"`) ou o `.txt` (linhas iniciadas por `>`).

#### `--source` num roteiro misto

`--source JA` declara que **tudo** é japonês. Mandar uma fala em inglês com
`sl=ja` faz o Google devolvê-la **intacta** — ele não consegue ler aquilo como
japonês — e o resultado é português salpicado de inglês.

A ferramenta separa sozinha: o idioma declarado vale para o que tem kana/kanji,
e o resto vai com detecção automática. Você vê isso no log:

```
--source JA: 412 textos sem kana/kanji vao com deteccao automatica
```

### 6. O que sobrou sem traduzir

```powershell
python stcm2l.py pending .\out -r --texts .\txt_ptbr -q
```

Roda na **saída** do inject e diz o que continua no idioma original — e, cruzando
com o arquivo de tradução, **por quê**:

| motivo | o que fazer |
|---|---|
| `nao foi extraido` | o bloco nunca virou entrada. Veja `diag` nesse arquivo |
| `sem traducao` | o filtro (`--only-cjk`/`--skip-ids`) preservou, ou o provedor pulou |
| `traducao igual ao original` | o tradutor devolveu o texto intacto — sintoma de `--source` errado |
| `nao injetado` | havia tradução e o `inject` não a escreveu (não coube, ou foi recusada) |

#### Quebra de linha (para a fala caber na tela)

Texto PT-BR é mais comprido que o japonês e estoura a caixa de diálogo. Por isso o
`translate` **já quebra as falas em 50 colunas por padrão**, gravando as quebras no
`.json` — dá para revisar e ajustar à mão antes de injetar.

```powershell
python stcm2l.py translate .\txt -o .\txt_ptbr --max-line 40   # caixa mais estreita
python stcm2l.py translate .\txt -o .\txt_ptbr --max-line 0    # desliga a quebra
```

O que a quebra respeita:

- **identificadores nunca são quebrados** (`NO00_0012`, `bgm_theme_01.at9`) — partir um
  deles faz o jogo perder o recurso;
- **marcadores saem inteiros** (`#Name[2]`, `{item}`, `%GOLD%`) e **não contam** na
  largura, porque o jogo não os desenha;
- **quebras que o texto já tinha são preservadas**, na forma em que estavam;
- palavra sozinha maior que o limite estoura a linha em vez de ser partida no meio;
- kana/kanji contam como **duas** colunas.

`--newline` escolhe a forma da quebra: `lf` grava o byte `0x0A`, `literal` grava a
sequência `\n` de dois caracteres. O padrão `auto` deduz do texto **original** do próprio
script — se as falas japonesas usam `0x0A`, a tradução usa `0x0A` também.

> A detecção do `auto` só é confiável no formato `.json`. No `.txt` as duas formas viram
> quebra real na leitura (`unesc`), então não há como distinguir uma da outra.

O mesmo par de flags existe no `inject`, como rede de segurança para texto editado à mão
depois do `translate` — a quebra é idempotente, então o que já veio quebrado não muda.

### 3. Injetar

```powershell
# um arquivo
python stcm2l.py inject .\scripts\EV_0001.DAT --texts .\txt_ptbr\EV_0001.json `
    -o .\out\EV_0001.DAT --out-encoding utf-8

# lote: casa cada .DAT com o .json/.txt de mesmo nome
python stcm2l.py inject .\scripts --texts .\txt_ptbr -o .\out --out-encoding utf-8
```

#### Dois modos, e a escolha muda o risco

| modo | o que faz | risco |
|---|---|---|
| **padrão** (cresce) | o bloco cresce e o arquivo inteiro é reendereçado | cabe qualquer tradução, mas depende de acertar **quais** words são ponteiro |
| **`--fit`** | nenhum bloco muda de tamanho; a sobra vira padding | **zero**: o arquivo sai com o mesmo tamanho e o mesmo layout, então não existe ponteiro para errar. O que não couber fica em japonês e é listado |

```powershell
python stcm2l.py inject .\scripts --texts .\txt_ptbr -o .\out --out-encoding utf-8 --fit
```

O `--fit` é a rede de segurança: se o jogo passou a se perder depois do patch,
reinjete com ele. Cada fala que não couber sai no relatório com **quantos bytes
cortar**; encurte a tradução no `.json` e rode de novo até zerar.

Mas `--fit` cobra caro: numa tradução JA→PT-BR típica, boa parte das falas não
cabe e fica em japonês. **Antes de encurtar centenas de linhas na mão**, injete
sem `--fit` e passe o `compare`: se ele fechar com `0 SUSPEITOS` e `0 ISOLADOS`,
a relocação está verificada e o arquivo que cresceu é bom. O `--fit` existe para
quando o `compare` acusa alguma coisa — ou quando o repack do jogo exige tamanho
fixo.

#### Identificador nunca é traduzido

O jogo procura voz, trilha e **o próximo script** por nome. Se `NO00_0012` virar
`Não 00_0012`, o jogo não acha o recurso e volta para o título. Por isso o `inject`
**recusa** tradução em bloco classificado como identificador e mantém o original,
avisando qual foi. `--allow-id-change` força (raramente é o que você quer).

#### `--fix-len` (desligado por padrão)

Alguns opcodes repetem o tamanho da string como imediato num parâmetro. O
`--fix-len` atualiza esses imediatos junto — mas é um **palpite**: qualquer
parâmetro que por acaso valha o tamanho antigo (um número de flag, um alvo de
salto) é reescrito junto e o roteiro desanda. Ligue só se souber que o seu título
precisa, e confira depois com o `compare`.

### 4. Encurtar o que não cabe na caixa

A caixa de texto do jogo aguenta um número fixo de **linhas**. O `--max-line` sempre
respeitou a *largura*, mas até então nada contava quantas linhas saíam — uma fala que
virava cinco linhas atravessava o pipeline inteiro e só aparecia cortada no jogo.
Como português é mais comprido que japonês, isso não é caso isolado: é estrutural.

```powershell
# 1. quanto estoura, SEM chamar a API nem gastar nada
python stcm2l.py shorten .\txt_ptbr -r --dry-run

# 2. encurtar de verdade
$env:GEMINI_API_KEY = "sua-chave"
python stcm2l.py shorten .\txt_ptbr -o .\txt_curto -r --cache .\shorten-cache.json

# 3. conferir que zerou
python stcm2l.py shorten .\txt_curto -r --dry-run
```

O orçamento é `--max-line` × `--max-lines` (padrão 50 × 3), com folga: a quebra por
palavra desperdiça o fim de cada linha, então o alvo declarado ao modelo é ~90% do
produto — 135 colunas, não 150. Pedir 150 devolve texto que fecha na conta e estoura
na quebra.

**Duas passadas.** Primeiro *reescrever*: a mesma informação e a mesma voz do
personagem, com menos palavras. O que continuar estourando vai para *resumir*, que pode
descartar detalhe secundário. O que nem assim couber sai com `needs_review` e uma nota
dizendo quanto falta — nada é truncado no meio da palavra.

> **Quem mede se coube é a ferramenta, nunca o modelo.** Ele não conhece a regra de
> quebra daqui. Toda resposta passa por `box_overflow` antes de ser aceita, e é essa
> conferência que decide se a fala vai para a segunda passada.

Marcadores (`#Name[2]`, `{var}`) viram placeholders antes de ir para o modelo e são
recolocados na volta, igual ao `translate`. Se algum se perder, a entrada é marcada
para revisão em vez de sair silenciosamente sem ele. Identificador nunca é encurtado.

| flag | |
|---|---|
| `--dry-run` | só conta e lista; não chama a API nem grava |
| `--ai-provider` | `gemini` (padrão, via `urllib`, sem dependência) ou `claude` (`pip install anthropic`) |
| `--ai-model` | nome errado faz o comando **listar os disponíveis** em vez de você adivinhar |
| `--cache` | re-rodar sai de graça; o orçamento entra na chave, então mudar a caixa re-encurta |
| `--api-key` | ou as variáveis `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` |

O `inject` também confere e **avisa** (nunca reescreve — isso faria o `.json` deixar de
descrever o que foi gravado, e é essa correspondência que o `compare` verifica).

> **Limite conhecido:** `wrap_text` nunca parte uma palavra, e japonês corrido não tem
> espaço. Uma fala que continuou em japonês fica numa linha só, por mais larga que seja.
> Isso vale para o que o `pending` lista como não traduzido, não para a saída em PT-BR.

### 5. Conferir o resultado

```powershell
python stcm2l.py verify .\out
python stcm2l.py extract .\out -o .\conferencia   # o texto PT-BR está lá?

# o que mudou ALÉM do texto?
python stcm2l.py compare .\scripts --patched .\out
```

O `verify` só prova que o parser reproduz o arquivo **quando nada muda de
tamanho**. Depois de injetar tradução maior, tudo se move e a ferramenta precisa
adivinhar quais words do script são ponteiro — e é aí que o roteiro quebra sem
erro nenhum aparecer.

O `compare` responde exatamente isso, comparando original e injetado elemento a
elemento e separando o que mudou em quatro baldes:

```
[ OK ] 600_sub_NO00.DAT
       tamanho    480 -> 596 (+116 bytes)
       elementos  6 -> 6
       textos     5 alterados (5 cjk)
       words      5 reescritos (5 relocacao esperada, 0 SUSPEITOS)
```

- **texto** — o que era para mudar mesmo;
- **identificador alterado** — nome de recurso/label que mudou: o jogo perde o recurso;
- **relocação esperada** — word que valia um endereço e passou a valer o endereço
  novo do **mesmo alvo lógico**: correto;
- **SUSPEITO** — word reescrito sem alvo lógico correspondente. É aqui que mora o
  bug de roteiro.

`--show-text` lista também as falas trocadas; `--limit N` controla quantos itens
aparecem por seção.

#### Slots isolados (o ponto cego dos "0 suspeitos")

Um imediato do jogo — número de flag, alvo de salto — que por acaso valha um
endereço conhecido é relocado junto, e o mapeamento dele bate **igualzinho** ao de
um ponteiro legítimo. Ou seja: ele sai como "relocação esperada" e o relatório
fecha com zero suspeitos, mesmo tendo corrompido o roteiro.

O que denuncia é a consistência: um slot que **é** ponteiro é relocado em quase
toda instância daquele opcode; uma coincidência aparece numa só.

```
       slots      12 (opcode, parametro, word) relocados: 11 consistentes, 1 ISOLADOS
       ! 1 SLOT(S) ISOLADO(S) - relocados em pouquissimas instancias do opcode:
           opcode 0x4BC param 0 word 2: 1/7 instancias (14%)
```

Slot isolado não é prova de erro — pode ser um ponteiro que só aparece em certos
casos. Quem decide é o comando `slots`.

### `slots` — qual word é *mesmo* ponteiro

```powershell
python stcm2l.py slots .\scripts -r
```

Roda só nos **originais** e separa as duas coisas que a relocação confunde:

- um **ponteiro** foi escrito para apontar, então praticamente toda instância dele
  cai exatamente sobre um endereço conhecido;
- um **imediato** do jogo (flag, alvo de salto, contador) às vezes cai sobre um
  endereço por acaso — e a frequência disso é justamente a **densidade de
  endereços** entre as posições 4-alinhadas do arquivo.

Por isso a conta não é "quantas foram relocadas" e sim **das que poderiam ser
ponteiro, quantas acertaram**:

```
Densidade media de enderecos entre as posicoes 4-alinhadas: 20.0%
  = a chance de um numero QUALQUER cair sobre um endereco por acaso.

    opcode  p  w  instancias  candidatas  acertos  precisao  veredito
  0x4BA     0  0          20          20       20   100.0%   PONTEIRO
  0x4BA     0  2          20           4        4    20.0%   acaso (imediato do jogo)
```

100% contra 20% — e 20% é exatamente a densidade. Não há ambiguidade.

### `inject --relocate slots`

De posse desse veredito, o `inject` reloca **só os slots que são ponteiro**:

```powershell
python stcm2l.py inject .\scripts --texts .\txt_ptbr -o .\out -r --relocate slots
```

É o modo que deixa o texto crescer sem o dano invisível do `scan`. Trecho cru só
é relocado quando o bloco inteiro parece tabela de ponteiros (≥90% dos words
caindo em endereços conhecidos), o que preserva a tabela do `GLOBAL_DATA` sem
reescrever dado solto.

Ordem de preferência, na prática: `--relocate slots` para crescer com segurança,
`--fit` quando o repack exigir tamanho fixo, `scan` só para investigar.

---

## "texto original divergente": o `.json` não descreve esse `.DAT`

O `inject` compara o campo `original` de cada entrada com o bloco que ela diz
ocupar. Quando os dois não batem, a entrada **não é injetada** — divergência
significa que o `original` descreve *outro* bloco, e escrever ali troca uma
string pela tradução de outra. Foi assim que `switch` virou `trocar` num script
e o jogo parou de achar a palavra-chave.

O aviso mostra os dois lados, e a forma deles diz a causa:

| o que aparece | o que é |
|---|---|
| `no .DAT está 'ꣁ@'`, `no arquivo de tradução '凜　'` | mojibake: a codificação de leitura está errada |
| `no .DAT está '”Œ¾ŽÒ–¼'` | cp932 lido como cp1252 — `発言者名` mojibakado |
| `no .DAT está 'switch'`, `no arquivo de tradução 'trocar'` | o `.json` foi extraído de uma saída **já injetada** |

Nos dois primeiros casos a ferramenta se corrige sozinha: ela mede em qual
codificação os `original` do `.json` batem com o `.DAT` e usa essa, avisando que
o meta estava errado. No terceiro não há conserto automático — o `.json` está
contaminado e precisa ser refeito a partir dos `.DAT` limpos.

`--ignore-mismatch` injeta mesmo assim; `--strict` aborta o arquivo inteiro.

---

## O jogo não sai da intro (ou volta para o título)

Sintoma clássico de script quebrado **sem crash**: a cena termina e a engine não
consegue seguir para a próxima, então volta para o começo. Em ordem de
probabilidade:

0. **O `.json` está contaminado** e injetou a tradução de um bloco em cima de
   outro (veja a seção acima). Uma palavra-chave da engine trocada — `switch` →
   `trocar` — basta para o roteiro parar de andar.
1. **Identificador traduzido.** O nome do próximo script, do arquivo de voz ou de
   uma label virou português. Rode
   `python stcm2l.py compare .\scripts --patched .\out` — identificador alterado
   aparece em destaque. Versões desta ferramenta anteriores a este guard traduziam
   isso quando o `translate` rodou **sem** `--only-cjk` / `--skip-ids`.
2. **Imediato reescrito por engano.** Um número do jogo (flag, alvo de salto) que
   por acaso valia um offset conhecido foi relocado junto. O `compare` marca como
   **SUSPEITO**. Solução definitiva: reinjetar com `--fit`.
3. **Arquivo repacotado com tamanho diferente.** Se o `.DAT` volta para dentro de
   um CPK/PAC, confira se a ferramenta de repack atualizou a TOC. Com `--fit` o
   tamanho não muda, o que elimina essa variável junto.

Roteiro de bissecção quando não dá para saber qual arquivo é o culpado:

```powershell
# 1. confirme que o jogo passa da intro com os .DAT originais
# 2. reinjete tudo em modo seguro e teste de novo
python stcm2l.py inject .\scripts --texts .\txt_ptbr -o .\out_fit --out-encoding utf-8 --fit
python stcm2l.py compare .\scripts --patched .\out_fit    # tem que dar 0 words reescritos
# 3. se com --fit o jogo anda, o problema era relocação; volte a crescer arquivo
#    por arquivo, rodando compare em cada um.
```

---

## A questão da codificação (leia antes de reclamar de "quadradinho" no jogo)

O script japonês costuma estar em **Shift-JIS (cp932)**, que **não possui** `á ã ç é ê ó`.
Por isso:

| Situação | O que fazer |
|---|---|
| O jogo lê UTF-8 | `--out-encoding utf-8` (o padrão dos scripts já em UTF-8) |
| O jogo lê só cp932 | `--fallback ascii` → grava `tradução` como `traducao`, e dobra a pontuação tipográfica (`—` → `-`, `«»` → `"`, `…` → `...`) que senão viraria `?` |
| Você editou a fonte/tabela do jogo | `--out-encoding <sua codificação>` |

Sem `--fallback`, a ferramenta **aborta com erro** em vez de gravar lixo — é proposital:
melhor falhar na hora do que descobrir o problema dentro do jogo.

Trocar a codificação de verdade (acentos nativos) exige mexer na fonte/tabela do jogo:
isso é trabalho de romhacking de assets, fora do escopo desta ferramenta.

---

## Marcadores da engine

Estes padrões são detectados, **protegidos durante a tradução automática** (nunca são
enviados ao tradutor) e restaurados depois:

```
#Name[2]   #KW_F[]   #KW_ED[]   #QualquerTag
{variavel}   <tag>   %VAR%   $VAR   \n \c \r \t
```

Se o tradutor destruir algum marcador, a entrada é marcada com
`"needs_review": true` e os marcadores perdidos são reanexados ao fim da linha —
nada é silenciosamente descartado.

---

## Formato STCM2L implementado

```
+0x00  char[0x20]  magic ("STCM2L ...", o texto varia por título)
+0x20  uint32      export_offset       offset absoluto da tabela de exports
+0x24  uint32      export_count
+0x28  uint32      collection_link     offset absoluto do marcador GLOBAL_DATA
+0x2C  uint32      desconhecido/padding
+0x30  ...         CODE_START_ | ações | CODE_END_ | GLOBAL_DATA | dados globais
 ...              tabela de exports (export_count * 0x28: char[0x20] nome + 2 uint32)
 ...              cauda: **também parseada** (vários títulos guardam ali um pool de strings)
```

**Ação:** `uint32 global_call, opcode, nparams, length` + `nparams * (3 * uint32)` + conteúdo extra.

**Bloco de dado — duas variantes**, detectadas por arquivo e reescritas na mesma forma:

| variante | cabeçalho | onde aparece |
|---|---|---|
| `classic` | `uint32 0, 1, padded_len, raw_len` | layout documentado; o tamanho útil é gravado |
| `wordcount` | `uint32 0, padded_len/4, 1, padded_len` | builds `STCM2L Apr 22 2013` (Otomate/Rejet). O tamanho útil **não** é gravado: a string é sempre terminada em `NUL` e o resto é padding zerado |

Nem todo arquivo tem `CODE_END_`. Quando falta, o código continua sendo uma corrida
de ações válidas: a ferramenta lê a corrida até ela acabar e trata o resto como área
de dados — em vez de inventar ações dentro do texto, como faria a varredura heurística.

### Como os ponteiros são recalculados

O corpo do arquivo é dividido em **elementos** (ações e trechos crus). Cada offset
alcançável — início de elemento, início de bloco de dado, início do payload — entra
num **mapa de endereços** em coordenadas lógicas (`elemento`, `segmento`, `deslocamento`),
não em aritmética fixa.

Na hora de gravar, os elementos são reposicionados com os tamanhos novos e **todo
uint32 cujo valor coincide exatamente com um endereço conhecido** é reescrito com o
destino novo. Valores imediatos (IDs, flags, `0xFFFFFFFF`) não batem com nenhum
endereço e passam intactos.

- `--relocate scan` (padrão): varre parâmetros, exports e trechos crus.
- `--relocate strict`: apenas o 1º word de cada parâmetro, exports e `collection_link`.
- `--no-fix-len`: desliga a correção de parâmetros que repetem o tamanho da string.

O código é delimitado pelos marcadores `CODE_START_` / `CODE_END_`; a área de dados
globais nunca é interpretada como código. Arquivos sem `CODE_START_` caem numa
varredura heurística com filtro anti-falso-positivo e um aviso no `info`.

A cauda (depois da tabela de exports) entra no mesmo mapa de endereços. É por isso
que um pool de strings guardado lá pode crescer: a tabela de exports é reposicionada
e os parâmetros que apontavam para o pool acompanham.

---

## Uso como biblioteca

```python
from pathlib import Path
from stcm2l import parse, build, collect_entries, detect_encoding

script = parse(Path("EV_0001.DAT").read_bytes())
enc = detect_encoding(script)

for entry in collect_entries(script, enc):
    print(entry.id, entry.original)

# edição direta de um bloco
elem, seg = 12, 0
script.elements[elem].segments[seg].set_content("Texto novo".encode("utf-8"))
Path("EV_0001_ptbr.DAT").write_bytes(build(script))
```

---

## Limitações conhecidas

- Não descompacta/descriptografa containers (`.PAK`, `.CPK`, `.FPK`). Extraia os `.DAT` antes.
- Não altera fontes nem tabelas de caracteres do jogo.
- Não interpreta a semântica dos opcodes — preserva tudo que não é texto, byte a byte.
- Se um `.DAT` só é entendido pela varredura heurística (aviso no `info`) e você
  redimensiona um bloco dentro de um trecho não reconhecido, a ferramenta avisa:
  um cabeçalho de ação escondido ali não teria o campo `length` atualizado.
- Caixa de texto: a quebra automática (`--max-line`, 50 por padrão) mede **colunas de
  texto**, não a largura real da fonte do jogo — numa fonte proporcional o número certo
  sai no olho, testando no jogo. O campo `max_bytes` de cada entrada traz o tamanho
  original em bytes como referência.

## Licença

MIT — veja [LICENSE](LICENSE).
