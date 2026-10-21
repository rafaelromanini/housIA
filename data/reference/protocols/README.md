# Protocolos de atenção primária (#28)

Um arquivo por eixo (`crianca`, `mulher`, `adulto_cronico`, `idoso`, `saude_mental`, `telessaude`). Cada arquivo tem
`eixo`, `fontes` (documento, órgão, ano e página ou seção) e `itens`.

Campos de um item: `id` (estável, usado pela sobrescrita local), `protocolo` (linha de cuidado), `item`, `fonte`
(chave de `fontes`; `pagina` do item vale mais que a do documento), `registro` (como o prontuário prova o item:
`lab:chave`, `texto:palavra|palavra`, `vacina:termo|termo`, `vital:pa:140/90`, `escala:phq9` ou `nenhum`),
`intervalo_dias` (0 = vale uma vez), `orientacao`, `fala` (palavras que, ditas na consulta, marcam o item como
abordado), `pergunta` (próxima pergunta sugerida enquanto o item estiver pendente), `critico`, `gatilhos_criticos` (ditos pelo paciente, com negação) e `gatilhos_pergunta` (valem só quando o
profissional pergunta e o paciente responde "sim").

Quem recebe o item: `condicao` (prefixos de CID-10), `sexo`, `idade_min`/`idade_max` (anos), `meses_min`/`meses_max`,
`trimestre` (pré-natal) e `aplica_se_fala` (entra quando a conversa cita o tema). Condição e fala valem como "ou";
idade e sexo sempre valem.

## Sobrescrita local

`local/*.yaml` (ou o diretório de `PROTOCOLOS_LOCAIS_DIR`) troca campos de um item pelo `id`, desliga com
`ativo: false` e acrescenta itens novos. O item sobrescrito aparece marcado como "local".

Arquivo local com erro (YAML quebrado, `id` novo sem os campos obrigatórios, valor inválido) é ignorado com aviso no log:
a base segue valendo. Itens críticos (risco de suicídio) aceitam só `item`, `protocolo`, `fonte`, `pagina`, `orientacao`,
`pergunta` e `intervalo_dias`; `ativo`, `critico`, gatilhos, idade e sexo do arquivo local são ignorados.

As páginas citadas são capítulos ou seções do documento: confira a página da edição que a instituição usa.
