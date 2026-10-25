# /brag — plano do vídeo (HousIA)

**Ferramenta:** /brag-slim (Opus 5.5) · **Formato:** 1920×1080, 30 fps · **Duração:** 24 s · **Idioma:** português
**Tom:** `polished` com direção "pitch comercial para hospitais": sério, elegante, confiante; holds longos, fades suaves.

## Respostas da inspeção

- **O que é:** IA que, na consulta e nas transições do paciente, trabalha com a fonte à vista: com o consentimento do
  paciente transcreve a conversa ao vivo, sugere perguntas, hipóteses e alertas e prepara a nota SOAP; também lê o
  prontuário (FHIR) na passagem de plantão, na transferência de setor e na alta.
- **Para quem:** hospitais — médicos, enfermagem e gestão. Tira do caminho o que se perde entre um turno e outro.
- **Diferencial:** cada afirmação da IA mostra a fonte no prontuário; regras determinísticas antes da IA; trava
  suave na alta de risco; conferência do que foi dito na passagem verbal.
- **Afirmação mais forte (verdadeira no produto):** "Item sem fonte não aparece" (validação de refs) e a
  conferência que pega "PA estável" × PA 184/102 em ascensão.
- **Gancho visual:** a frase verbal "PA estável, sem intercorrências." sendo contradita por um card crítico real.
- **UI real usada:** HTML e CSS capturados do app rodando, com textos gerados pelo Claude no cenário de demonstração
  (pacientes fictícios: Antônio, Iracema, Sebastião).
- **Legenda de compartilhamento:** ver `share-copy.txt`.

## Identidade

Atkinson Hyperlegible (fonte do app), primário `#0e7490`, fundo `#f3fbfc`, texto `#12404f`, severidade crítica
`#b91c1c` / `#fef2f2`, atenção `#92400e` / `#fffbeb`. Ícone de estetoscópio (lucide) do cabeçalho do app.

## Storyboard

| # | Tempo | Cena | Na tela | Texto para ler |
|---|---|---|---|---|
| 1 | 0,0–4,0 s | **Gancho** | Fundo teal escuro. Balão de fala sendo digitado: *“PA estável, sem intercorrências.”* Em 1,2 s entra o card crítico real da conferência verbal ("PA 184/102 mmHg (CRÍTICO)"). | "O que não é dito na passagem, se perde." (8 palavras, 2,6 s) |
| 2 | 4,0–7,0 s | **Revelação** | Logo HousIA + painel de leitos real desfocado ao fundo. | "IA para a transição do cuidado" + "Passagem de plantão · Transferência · Alta" |
| 3 | 7,0–12,0 s | **Destaque 1** | Headline real da passagem; riscos entram um a um; cursor clica em "ver fonte" e abre o registro FHIR. | "Passagem de plantão por perfil" / "Cada alerta com a fonte no prontuário." |
| 4 | 12,0–16,2 s | **Destaque 2** | Dois alertas críticos reais (alergia à dipirona; morfina + diazepam) e a caixa "Hipótese da IA — confirmar". | "Reconciliação na transferência" / "Alergias, interações e omissões antes da dose." |
| 5 | 16,2–20,4 s | **Destaque 3** | Card real "Alta travada" (risco ALTO) e as orientações ao paciente em espanhol deslizando ao lado. | "Alta segura" / "Alta de risco só sai com plano." |
| 6 | 20,4–24,0 s | **Fechamento** | Logo, punchline e selos. | "Nenhum alerta sem fonte. Nenhuma transição no escuro." + "Piloto com o seu hospital — vamos conversar." |

## Som

Trilha sintetizada em Ré maior, 100 BPM, I–V–vi–IV (pad + pluck + baixo + kick/hat suaves). Efeitos no mesmo tom
e espaço, abaixo da música: teclas suaves na digitação, um "swell" antes da revelação, clique no "ver fonte",
whooshes curtos nas trocas de cena. Fade final no acorde de Ré.

## Cuidados

Sem segredos, chaves, URLs internas ou dados reais: só dados sintéticos do cenário de demonstração. Nenhum número,
depoimento ou cliente inventado.

## Versão 2: foco na Consulta com IA

O roteiro acima (24 s, passagem/transferência/alta) segue valendo como corte curto. Para o pitch centrado na
**Consulta com IA**, use a sequência abaixo (cerca de 40 a 60 s). Tudo vem do app rodando com dados sintéticos (paciente
do cenário `cen6`); nenhuma afirmação de ganho, de conformidade ou de certificação (o produto **apoia**, não certifica).

| # | Cena | Na tela | Texto para ler |
|---|---|---|---|
| 1 | **Gancho** | Médico olhando o paciente; tela com o aviso de consentimento marcado e "Iniciar". | "A consulta é sobre o paciente, não sobre o teclado." |
| 2 | **Ao vivo** | Transcrição entrando; perguntas sugeridas saem da lista quando o profissional as faz. | "Transcrição ao vivo, com consentimento." |
| 3 | **Com fonte** | Alertas de segurança e hipóteses, cada um com `Fala/N` ou o recurso FHIR; aba Medicamentos e alergias. | "Cada alerta mostra de onde veio." |
| 4 | **Nota** | Nota SOAP citando as falas; profissional revisa, atesta e envia ao prontuário. | "O profissional revisa. A nota só vai com a aprovação dele." |
| 5 | **Confiança** | Dossiê de conformidade e auditoria encadeada (telas `/conformidade` e `/auditoria`); menção a IA substituível e pseudonimização, autorização prévia e perfis BR/e-SUS (sem alegar certificação). | "Privacidade e rastro de auditoria desde o desenho." |
| 6 | **Fechamento** | Logo e convite. | "Piloto com o seu hospital: vamos conversar." |

### Como produzir

Os scripts usam caminhos relativos ao repositório (funcionam no Windows e no Linux) e a URL do app vem de `BASE_URL`
(padrão `http://localhost:3000`). Com API e web no ar (`LLM_MODE=mock`, `FHIR_MODE=local`; se a web não estiver na porta
3000, inclua a origem dela em `CORS_ORIGINS` da API):

```bash
# gravação direta da demo (WebM), saída fora do git
BASE_URL=http://localhost:3000 OUT_DIR=/caminho/fora/do/repo node brag-output/scripts/record-consult.mjs
ffmpeg -i /caminho/fora/do/repo/*.webm -c:v libx264 -pix_fmt yuv420p -an demo-consulta.mp4

# composição motion (trechos reais da UI -> quadros): capture-consult.mjs, capture.mjs, build_composition.py, render.mjs
node brag-output/scripts/capture-consult.mjs   # grava em brag-output/work/ (ignorado pelo git)
```

Não versione vídeos grandes: guarde fora do repositório.
