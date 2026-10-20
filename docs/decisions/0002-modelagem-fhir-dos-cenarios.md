# 0002 — Modelagem FHIR dos cenários de transição

**Status:** aceita · **Fase:** 1

## Decisões
1. **DSL em PT-BR com horários relativos** (`data/scenarios/*.json`, formato em `data/scenarios/README.md`).
   Todo horário é relativo a uma âncora ("agora" na carga): `-3d2h`, `-25m`. A demo sempre tem dados recentes
   e a janela de 12h da passagem de plantão sempre contém os eventos roteirizados.
2. **Setores como sub-encounters**: a internação é um `Encounter` (class IMP) e cada passagem por setor é um
   `Encounter` com `partOf` apontando para a internação e `location` = leito (`Location` com `partOf` = ala).
   Isso dá a origem/destino de cada transferência de forma nativa.
3. **Prescrição por setor**: `MedicationRequest.encounter` aponta para o sub-encounter do setor. Ao sair do setor,
   a prescrição de origem fica `completed` com `boundsPeriod.end` = horário da transferência. Uma suspensão
   explícita é `status=stopped` + `statusReason` + `boundsPeriod.end`. Uma troca usa `priorPrescription`.
   É isso que permite à reconciliação distinguir **suspensão registrada** de **omissão**.
4. **Administrações geradas a partir do aprazamento** (`timing.repeat.timeOfDay`), para não haver contradição
   entre horários prescritos e administrados quando a âncora muda.
5. **Codificação de medicamentos**: além do ATC, cada medicamento leva um código do sistema de demonstração
   `http://housia.dev/fhir/CodeSystem/principio-ativo`, ligado a `data/reference/drugs.csv` (classe terapêutica).
   Num hospital real isso seria um mapeamento do código local para princípio ativo.
6. **Moradores do domicílio**: `Observation` LOINC 63512-8 (pré-preenche "mora sozinho" no checklist de alta).
   Cuidador principal: `RelatedPerson.relationship` com a tag `cuidador` do sistema de demonstração.
7. **Synthea opcional**: a população de fundo (19 pacientes) vem de um gerador determinístico próprio
   (`app/seed/background.py`) para não exigir Java/Synthea na demo. Synthea pode ser usado em cima disso
   carregando os bundles dele no mesmo HAPI.

## Consequências
- IDs FHIR determinísticos (`cen1-rx-losartana-enf`), carga idempotente via `PUT`.
- Códigos de escalas sem LOINC confirmado (Braden) usam sistema local de demonstração.
