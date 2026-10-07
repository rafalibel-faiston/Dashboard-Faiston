"""Regras de turno e jornada, sem banco -- funções puras pra dar pra testar
sem Postgres.

Turno noturno (19h-07h) atravessa a meia-noite: o "turno de hoje" de quem
está às 02h começou ontem às 19h. Toda conta de KPI "do turno" usa a
janela devolvida aqui, não o dia civil.
"""
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Sao_Paulo")

# Status do operador. Pausa tem motivo (banheiro, almoço, feedback...).
STATUS_OPERADOR = ("online", "pausa", "indisponivel")

# Evento aberto além do fim do turno + tolerância é esquecido (a pessoa foi
# embora em pausa) -- o sistema antigo chegava a mostrar 96h de pausa.
TOLERANCIA_ENCERRAMENTO = timedelta(minutes=30)
DURACAO_PADRAO_SEM_JORNADA = timedelta(hours=12)


def agora() -> datetime:
    """Horário de Brasília sem tzinfo, mesmo formato das colunas TIMESTAMP."""
    return datetime.now(FUSO).replace(tzinfo=None)


# Quem chega até 2h antes já está no turno que vai começar (conta pro KPI
# dele); antes disso, o que for registrado ainda é hora extra do anterior.
ANTECEDENCIA = timedelta(hours=2)


def _inicio_turno(ref: datetime, inicio: time) -> datetime:
    ini = datetime.combine(ref.date(), inicio)
    if ini - ANTECEDENCIA > ref:
        ini -= timedelta(days=1)
    return ini


def _duracao(inicio: time, fim: time) -> timedelta:
    d = datetime.combine(date.min, fim) - datetime.combine(date.min, inicio)
    return d if d > timedelta(0) else d + timedelta(days=1)


def janela_turno(ref: datetime, inicio: Optional[time], fim: Optional[time]) -> Tuple[datetime, datetime]:
    """Horário oficial (início, fim) do turno ao qual `ref` pertence: o que
    está rolando, o que começa em até 2h, ou o último que já terminou.
    Sem jornada cadastrada vale o dia civil."""
    if inicio is None or fim is None:
        ini = datetime.combine(ref.date(), time(0, 0))
        return ini, ini + timedelta(days=1)
    ini = _inicio_turno(ref, inicio)
    return ini, ini + _duracao(inicio, fim)


def janela_contagem(ref: datetime, inicio: Optional[time], fim: Optional[time]) -> Tuple[datetime, datetime]:
    """Janela usada pra contar atendimento "do turno" (KPI e lista). As
    janelas são contíguas -- de 2h antes do início até 2h antes do próximo
    turno -- pra hora extra e chegada antecipada nunca sumirem da lista."""
    if inicio is None or fim is None:
        return janela_turno(ref, inicio, fim)
    ini = _inicio_turno(ref, inicio)
    return ini - ANTECEDENCIA, ini + timedelta(days=1) - ANTECEDENCIA


def fim_esperado_evento(inicio_evento: datetime, j_ini: Optional[time], j_fim: Optional[time]) -> datetime:
    """Até quando um status aberto em `inicio_evento` faz sentido ficar aberto."""
    if j_ini is None or j_fim is None:
        return inicio_evento + DURACAO_PADRAO_SEM_JORNADA
    _, fim = janela_turno(inicio_evento, j_ini, j_fim)
    if fim < inicio_evento:  # abriu depois do fim do turno (hora extra)
        return inicio_evento + TOLERANCIA_ENCERRAMENTO
    return fim


def deve_encerrar(inicio_evento: datetime, j_ini: Optional[time], j_fim: Optional[time],
                  ref: Optional[datetime] = None) -> Optional[datetime]:
    """Se o evento ficou esquecido aberto, devolve o horário em que deve ser
    fechado (fim do turno); senão None."""
    ref = ref or agora()
    fim = fim_esperado_evento(inicio_evento, j_ini, j_fim)
    if ref > fim + TOLERANCIA_ENCERRAMENTO:
        return fim
    return None


def dias_12x36(data_inicio: date, data_fim: date):
    """Dias trabalhados num 12x36 a partir de `data_inicio` (dia sim, dia não)."""
    d = data_inicio
    while d <= data_fim:
        yield d
        d += timedelta(days=2)
