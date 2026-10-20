# Como criar um Smart App

Um Smart App novo precisa de **um módulo, um CSV e um teste**. Não se mexe no engine, no schema nem na tela: o registro
(`apps/api/app/consult/apps/`) descobre o módulo, valida o manifest e a tela desenha o card pelo tipo declarado.

## 1. O CSV (opcional, mas é onde a curadoria mora)

Coloque a base em `data/reference/` com uma coluna `fonte` em toda linha. Dados sintéticos ou de demonstração dizem isso
na própria fonte. Exemplo, `data/reference/smart_apps_tabagismo.csv`:

```csv
cid,orientacao,fonte
Z72.0,Oferecer apoio para parar de fumar,Protocolo de demonstração
```

## 2. O módulo

Crie `apps/api/app/consult/apps/tabagismo.py` com `MANIFEST` e `run(ctx)`:

```python
from app.consult.apps.base import AppContext, BaseCurada, Gatilho, Manifest, Saida
from app.consult.schemas import AppItem
from app.reference import _read_csv

MANIFEST = Manifest(
    id="tabagismo",                      # minúsculas, números e _
    nome="Tabagismo",
    aba="prontuario",                    # vivo | raciocinio | medicamentos | prontuario | pedidos | transicao
    gatilho=Gatilho(tipos=["consulta"], idade_min=18),   # quando roda (tipo, internado, sexo, idade)
    especialidades=[],                   # vazio = todas
    recursos_fhir=["Condition"],         # o que o app lê do FHIR
    base=BaseCurada(arquivo="smart_apps_tabagismo.csv", versao="1.0.0", fonte="Protocolo de demonstração"),
    camada="regra",                      # regra | ia | mista
    saida=Saida(card="lista"),           # lista | tags | tabela (com colunas) | linha_do_tempo
    versao="1.0.0",
)


def run(ctx: AppContext) -> list[AppItem] | None:
    cids = {icd for icd, _, _ in ctx.chart.conditions}
    return [
        AppItem(texto=r["orientacao"], severidade="info", refs=[ctx.patient_ref])
        for r in _read_csv("smart_apps_tabagismo.csv")
        if r["cid"] in cids
    ]
```

Regras do contrato:

- `run(ctx)` devolve itens com `refs`: recursos FHIR do paciente (`Condition/abc`) e/ou falas (`Fala/3`). Item sem
  referência válida é descartado pelo registro (vai para `descartados` do resultado) e nunca chega à tela.
- Devolva `None` quando o app não se aplica a este paciente (o card some); devolva `[]` quando se aplica mas não há
  nada a apontar (o card mostra `vazio`).
- `ctx` traz `ctx.ins` (os Insights do engine: alertas, medicamentos, alergias...), `ctx.chart` (o prontuário já lido:
  exames, sinais vitais, diagnósticos), `ctx.segments` (a conversa), `ctx.extra` e `ctx.record`.
- O app só lê: as regras de segurança do engine continuam rodando antes e não dependem de nenhum app.
- App que lança exceção vira um card com aviso; o resto da consulta segue normal.
- Card `tabela`: cada item traz uma célula por coluna do manifest. Uma coluna chamada `Situação` vira selo colorido.
- Camada `ia`: a saída da IA passa pelo mesmo filtro de referências e vem marcada com `origem="ia"`.

## 3. O teste

O teste carrega o módulo pelo registro e passa pelo caminho real (veja `apps/api/tests/test_smart_apps.py`, teste
`test_example_app_enters_with_one_module_one_csv`, que cria um app de exemplo em uma pasta temporária e o vê aparecer em
`GET /smart-apps` e nos insights de uma consulta):

```python
def test_tabagismo(record):
    ctx = context(record, "cen6")
    result = apps.run_one(apps.discover()["tabagismo"], ctx, None)
    assert result.itens == []   # a paciente sintética não tem Z72.0
```

## 4. O que a plataforma faz por você

- `GET /smart-apps` lista o catálogo com o manifest (e se o perfil do usuário esconde o app).
- Perfis no servidor: `PUT /smart-apps/perfil` guarda os apps escondidos por usuário e especialidade; o que se esconde
  vale em qualquer aparelho. App novo aparece para todos até alguém escondê-lo.
- Plug-in local fora do pacote: `apps.load_file(Path("meu_app.py"))` registra um módulo avulso, com o CSV na mesma pasta.
- Ordem na aba: campo `ordem` do manifest (menor primeiro).
