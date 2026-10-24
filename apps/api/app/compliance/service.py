"""Dossiê de conformidade vivo (#53): RoPA, RIPD resumido e matriz LGPD/CFM.

A parte estática vem de `data/compliance/dossie.yaml` (versionada com o código); a parte calculada vem do sistema agora
(retenção configurada, cadeia do audit_log, quebras de vidro, provedor de IA ativo). O dossiê descreve controles; não
atesta conformidade: o que depende de interpretação jurídica sai marcado para revisão humana.
"""

from datetime import datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.audit import chain
from app.config import get_settings
from app.consult import acesso, privacidade
from app.db.models import AuditLog, User

STATUS_ROTULO = {"implementado": "implementado", "parcial": "parcial", "nao_implementado": "NÃO implementado"}


def estatico() -> dict:
    path = get_settings().data_dir / "compliance" / "dossie.yaml"
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _quebras(db: Session, desde: datetime | None = None) -> int:
    conds = [AuditLog.action == acesso.ACAO_QUEBRA]
    if desde is not None:
        conds.append(AuditLog.created_at >= desde)
    return db.scalar(select(func.count()).select_from(AuditLog).where(*conds)) or 0


def calculado(db: Session, user: User) -> dict:
    s = get_settings()
    agora = clock.now()
    cadeia = chain.verificar(db, user.organization_id)
    real = s.llm_mode == "real"
    dias = privacidade.retention_days()
    return {
        "gerado_em": agora.isoformat(),
        "retencao": {
            "transcricao_dias_por_tipo": dias,
            "configurada": any(v is not None for v in dias.values()),
            "audit_log": "sem expurgo automático",
        },
        "cadeia_audit_log": {
            k: cadeia[k] for k in ("integra", "total", "encadeadas", "anteriores_ao_encadeamento", "primeira_quebra")
        },
        "acessos_quebra_de_vidro": {
            "total": _quebras(db),
            "ultimos_90_dias": _quebras(db, agora - timedelta(days=90)),
        },
        "criptografia_de_campo": bool(s.data_encryption_key),
        "provedor_de_llm": {
            "modo": s.llm_mode,
            "provedor": "Anthropic" if real else None,
            "modelo": s.llm_model if real else None,
            "regiao": (s.llm_region or "não informada") if real else None,
        },
        "suboperadores_ativos": [
            *(["Provedor de LLM (Anthropic)"] if real else []),
            *(["Servidor de transcrição (STT_URL)"] if s.stt_url else []),
            *(["Sistema de prontuário (escrita FHIR)"] if s.ehr_writeback else []),
            *(["Servidor SMTP"] if s.smtp_host else []),
        ],
        "stt_do_navegador_permitido": s.allow_browser_stt,
    }


def montar(db: Session, user: User) -> dict:
    base = estatico()
    matriz = base["matriz"]
    por_status: dict[str, int] = {}
    for m in matriz:
        por_status[m["status"]] = por_status.get(m["status"], 0) + 1
    revisao = [
        x.get("id") or f"{x['norma']} {x['artigo']}" for x in (*base["ropa"], *matriz) if x.get("revisao_juridica")
    ]
    return {
        "versao": base["versao"],
        "atualizado_em": base["atualizado_em"],
        "aviso": base["aviso"],
        "papeis": base["papeis"],
        "ropa": base["ropa"],
        "ripd": base["ripd"],
        "matriz": matriz,
        "resumo_matriz": por_status,
        "precisa_revisao_juridica": revisao,
        "calculado": calculado(db, user),
    }


def _lista(itens) -> str:
    return ", ".join(itens) if itens else "—"


def markdown(d: dict) -> str:
    c = d["calculado"]
    cad = c["cadeia_audit_log"]
    llm = c["provedor_de_llm"]
    out = [
        f"# Dossiê de conformidade HousIA — versão {d['versao']}",
        "",
        f"Conteúdo estático de {d['atualizado_em']}; partes calculadas em {c['gerado_em']}.",
        "",
        f"> {d['aviso']}",
        "",
        "## Estado do sistema (calculado)",
        "",
        f"- Cadeia do audit_log: {'íntegra' if cad['integra'] else 'QUEBRADA'} ({cad['encadeadas']} encadeadas, "
        f"{cad['anteriores_ao_encadeamento']} anteriores ao encadeamento, {cad['total']} no total)",
        f"- Acessos por quebra de vidro: {c['acessos_quebra_de_vidro']['total']} "
        f"({c['acessos_quebra_de_vidro']['ultimos_90_dias']} nos últimos 90 dias)",
        f"- Retenção da transcrição: {c['retencao']['transcricao_dias_por_tipo']}",
        f"- Retenção do audit_log: {c['retencao']['audit_log']}",
        f"- Criptografia de campo: {'ligada' if c['criptografia_de_campo'] else 'DESLIGADA'}",
        f"- Provedor de LLM: {llm['provedor'] or 'nenhum (modo ' + llm['modo'] + ')'}",
        f"- Suboperadores ativos: {_lista(c['suboperadores_ativos'])}",
        "",
        "## RoPA — operações de tratamento (LGPD art. 37)",
        "",
    ]
    for o in d["ropa"]:
        out += [
            f"### {o['id']} — {o['operacao']}",
            "",
            f"- Dados: {o['dados']}",
            f"- Titulares: {o['titulares']}",
            f"- Finalidade: {o['finalidade']}",
            f"- Base legal (proposta): {o['base_legal']}",
            f"- Retenção: {o['retencao']}",
            f"- Operadores e suboperadores: {_lista(o['operadores'])}",
            f"- Onde no código: {_lista(o['codigo'])}",
            f"- Revisão jurídica humana: {'necessária' if o.get('revisao_juridica') else 'não marcada'}",
            "",
        ]
    r = d["ripd"]
    out += ["## RIPD resumido (LGPD art. 38)", "", f"Escopo: {r['escopo']}", "", f"Fluxo: {r['fluxo']}", ""]
    for x in r["riscos"]:
        out.append(f"- **{x['risco']}**. Medida: {x['medida']} Risco residual: {x['residual']}")
    out += ["", "Pendências:", *[f"- {p}" for p in r["pendencias"]], "", "## Matriz CFM e LGPD", ""]
    out += ["| Norma | Artigo | Exigência | Controle | Status | Código | Testes |", "|---|---|---|---|---|---|---|"]
    for m in d["matriz"]:
        rev = " (revisão jurídica)" if m.get("revisao_juridica") else ""
        out.append(
            f"| {m['norma']} | {m['artigo']} | {m['exigencia']} | {m['controle']} | "
            f"{STATUS_ROTULO.get(m['status'], m['status'])}{rev} | {_lista(m['codigo'])} | {_lista(m['testes'])} |"
        )
    return "\n".join(out) + "\n"
