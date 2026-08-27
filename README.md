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
> - `--skip-ids` — script **em inglês**: traduz fala em qualquer idioma, mas preserva o que
>   tem cara de identificador. Num script em inglês, `--only-cjk` pularia tudo.
>
> O que for pulado fica com `translation` vazio, e o `inject` preserva o original.

Para descobrir o idioma de cada arquivo antes de traduzir, use o `info`: a linha
`conteudo` separa japonês, prosa latina e identificadores, e mostra se o texto está
embutido nas ações ou num pool solto.

**Manual:** edite o `.json` (campo `"translation"`) ou o `.txt` (linhas iniciadas por `>`).

### 3. Injetar

```powershell
# um arquivo
python stcm2l.py inject .\scripts\EV_0001.DAT --texts .\txt_ptbr\EV_0001.json `
    -o .\out\EV_0001.DAT --out-encoding utf-8

# lote: casa cada .DAT com o .json/.txt de mesmo nome
python stcm2l.py inject .\scripts --texts .\txt_ptbr -o .\out --out-encoding utf-8
```

### 4. Conferir o resultado

```powershell
python stcm2l.py verify .\out
python stcm2l.py extract .\out -o .\conferencia   # o texto PT-BR está lá?
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
- Caixa de texto: nada garante que uma frase mais longa **caiba na tela**. O campo
  `max_bytes` de cada entrada traz o tamanho original como referência.

## Licença

MIT — veja [LICENSE](LICENSE).
