"""
Agendador unificado de publicacoes — YouTube + Facebook + Instagram.

Uso:
    py agendador_publicacoes.py
        Sem argumento nenhum: lista os lotes disponiveis em _agendamentos/ e
        pergunta qual rodar.

    py agendador_publicacoes.py caminho\\lote.json
        Roda esse lote direto (sem perguntar).

    py agendador_publicacoes.py caminho\\lote.json --delay-min 30 --delay-max 90
        Mesma coisa, mudando o intervalo aleatorio entre uma acao e outra
        (em segundos). Padrao: 20 a 60s (atualizado 23/07/2026 — antes era
        240-540s/4-9min, a Jaqueline reclamou que tava demorando horas).
        So se aplica quando duas chamadas seguidas repetem o MESMO canal E a
        MESMA plataforma (ver ORDEM_CANAIS em processar_lotes) — trocar de
        canal ou de plataforma ja evita repetir token, sem precisar de delay.

    py agendador_publicacoes.py --check-pendentes <canal>
        Confere comentarios pendentes (YouTube + Facebook) desse canal e posta
        os que ja podem ser postados. Rodar SEMPRE no inicio de toda sessao,
        antes de rodar um lote novo.

--------------------------------------------------------------------------
COMO FUNCIONA (design decidido em 2026-07-08, junto com a Jaqueline)
--------------------------------------------------------------------------
YouTube e Facebook tem agendamento NATIVO: a propria plataforma guarda o
video/post e publica sozinha na hora certa. Por isso, pra esses dois, o
script dispara a chamada de API JA (assim que chega a vez do job na fila),
configurada pra publicar na hora certa, e segue pro proximo job — com um
delay aleatorio entre uma chamada e outra so pra nao parecer bot disparando
tudo de uma vez.

Instagram NAO tem agendamento nativo (a API so publica imediatamente).
Por isso, pra Instagram, o script fica de pe ESPERANDO (dormindo) ate o
horario exato de cada job, e so entao publica de verdade. Por isso a
recomendacao e deixar o PC ligado com o script rodando o dia inteiro nos
dias que tem post de Instagram.

Cada job tem um "status" que o PROPRIO SCRIPT regrava no arquivo JSON assim
que a acao e concluida (pendente -> agendado / publicado, ou erro: <msg>).
Rodar o mesmo arquivo de novo NUNCA reposta o que ja foi feito — o script
avisa quais jobs ja foram processados e pula eles, so mexendo nos que ainda
estao "pendente".

--------------------------------------------------------------------------
GAP CONHECIDO — Instagram foto/carrossel
--------------------------------------------------------------------------
A API do Instagram (Content Publishing) SO aceita imagem via URL publica
(image_url) — nao aceita upload direto de arquivo local, diferente de
video (Reels), que aceita upload resumable direto do PC. Por isso:
  - Instagram Reels (video)      -> IMPLEMENTADO (upload direto, sem hospedagem)
  - Instagram foto/carrossel     -> NAO IMPLEMENTADO (precisa decidir antes:
                                     onde hospedar a imagem pra gerar uma URL
                                     publica temporaria — ex: Netlify, algum
                                     bucket, etc. Decidir isso quando o
                                     conteudo de carrossel estiver pronto pra
                                     postar de verdade.)
Facebook foto FUNCIONA normal (upload direto, /page-id/photos aceita arquivo).
Facebook nao tem "carrossel" nativo (so Instagram tem esse formato).

--------------------------------------------------------------------------
AVISO — Facebook Reels e Instagram Reels sao codigo NOVO, ainda nao testado
--------------------------------------------------------------------------
O caminho do YouTube e o mesmo de sempre (ja provado, ja rodou de verdade).
Os caminhos de Facebook Reels e Instagram Reels foram escritos direto a
partir da documentacao oficial da Meta (protocolo de upload resumable via
rupload.facebook.com) mas NUNCA rodaram contra a API de verdade ainda.
Primeira rodada real: acompanhar de perto, tratar como teste — se der erro
de formato de campo/header, e mais facil de ajustar vendo o erro real da
API do que adivinhando.

--------------------------------------------------------------------------
SCHEMA DO ARQUIVO DE LOTE
--------------------------------------------------------------------------
{
  "lote": "Metodo ANA - semana de 14/07",
  "jobs": [
    {
      "id": "MA_S04_YT",                  // identificador unico dentro do arquivo (so pra rastrear/logar)
      "canal": "metodoana",               // metodoana | sacrarium | protocolo4d | forjandotitas
      "plataforma": "youtube",            // youtube | facebook | instagram
      "tipo": "video",                    // video (YT longo) | reels (YT Shorts/FB/IG) | foto (FB)
      "publish_at": "2026-07-14T12:00:00-03:00",  // ISO 8601 COM fuso explicito (BRT = -03:00)
      "status": "pendente",               // o script regrava sozinho: pendente -> agendado/publicado/erro: ...

      // --- campos especificos YouTube ---
      "video_path": "D:\\...\\S04_....mp4",
      "thumb_path": "D:\\...\\S04_....jpeg",   // opcional -- se o video tiver varias opcoes "Teste A/B/C"
                                               // geradas na pasta de thumbnails, quem monta este job
                                               // (agente_postagem.md) deve apontar pra "Teste A" por
                                               // padrao, a menos que Jaqueline peca outra letra -- este
                                               // script so recebe o caminho ja escolhido, nao decide.
      "title": "...", "description": "...", "tags": ["..."], "category_id": 27,
      "pinned_comment": "..."             // opcional — vai pra fila de pendentes (nunca comentado na hora)

      // --- campos especificos Facebook (tipo=video/reels) ---
      // "video_path": "...", "caption": "...", "first_comment": "..." (opcional)
      // --- campos especificos Facebook (tipo=foto) ---
      // "image_path": "...", "caption": "...", "first_comment": "..." (opcional)

      // --- campos especificos Instagram (tipo=reels) ---
      // "video_path": "...", "caption": "...", "first_comment": "..." (opcional)
    }
  ]
}

Regra de link: no Facebook (reels, foto), o link do produto NUNCA vai no
"caption" — sempre no first_comment. No Instagram tambem, pelo mesmo motivo
de nao afastar o algoritmo de posts com link no corpo.
"""
import os
import sys
import json
import re
import time
import random
import argparse
import subprocess
import tempfile
import datetime as dt
from pathlib import Path

import requests
from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).parent))
import youtube_upload as ytmod
import sincronizar_midia_nuvem as syncmod

# --------------------------------------------------------------------------
# NUVEM (28/07/2026): quando roda no GitHub Actions (sem D:\ local nem
# C:\Users\Jaqueline\.claude\tokens), os caminhos mudam:
#   - TOKENS_DIR aponta pra uma pasta temporaria que o workflow escreve a
#     partir dos GitHub Secrets antes de chamar este script (ver
#     .github/workflows/postar.yml).
#   - video_path/image_path/image_paths dos jobs podem ser URLs publicas
#     (http/https) do Supabase em vez de caminho local — ver
#     _resolver_midia_local() mais abaixo, que baixa pra um arquivo
#     temporario quando precisar. sincronizar_midia_nuvem.py e quem sobe os
#     arquivos e grava esses campos *_nuvem nos lotes ANTES do workflow
#     rodar.
# Local (PC da Jaqueline) continua funcionando exatamente igual, sem nenhuma
# mudanca de comportamento — so ativa o modo nuvem se a env var existir.
TOKENS_DIR = Path(os.environ.get("AGENDADOR_TOKENS_DIR", r"C:\Users\Jaqueline\.claude\tokens"))
JOBS_DIR = Path(__file__).parent / "_agendamentos"
HISTORICO_PATH = Path(__file__).parent / "_historico" / "historico_postagens.json"
GRAPH = "https://graph.facebook.com/v21.0"
BRT = dt.timezone(dt.timedelta(hours=-3))

CONTA_FB = {
    "metodoana": "cofrinhodefe",
    "sacrarium": "sacrarium",
    "protocolo4d": "protocolo4d",
}
CONTA_IG = {
    "metodoana": "cofrinhodefe",
    "sacrarium": "sacrarium",
    "protocolo4d": "forjandotitas",
    "forjandotitas": "forjandotitas",
}


# --------------------------------------------------------------------------
# http com erro real (raise_for_status() sozinho perde o corpo da resposta —
# e exatamente onde a Meta explica o motivo real do 400/401/etc)
# --------------------------------------------------------------------------
def _checar(resp):
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} {resp.reason} em {resp.url} -> {resp.text[:2000]}")
    return resp


# --------------------------------------------------------------------------
# credenciais
# --------------------------------------------------------------------------
def _read_token_file(nome):
    path = TOKENS_DIR / nome
    d = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def _fb_creds(canal):
    conta = CONTA_FB.get(canal)
    if not conta:
        raise ValueError(f"canal '{canal}' nao tem conta de Facebook mapeada (ou nao posta no Facebook)")
    d = _read_token_file(f"facebook_{conta}.txt")
    return d["FACEBOOK_PAGE_ID"], d["PAGE_ACCESS_TOKEN"]


def _ig_creds(canal):
    conta = CONTA_IG.get(canal)
    if not conta:
        raise ValueError(f"canal '{canal}' nao tem conta de Instagram mapeada (ou nao posta no Instagram)")
    d = _read_token_file(f"instagram_{conta}.txt")
    return d["INSTAGRAM_BUSINESS_ID"], d["ACCESS_TOKEN"]


# --------------------------------------------------------------------------
# NUVEM — resolver midia local a partir de URL (Supabase), quando o arquivo
# nao existe no disco (caso do GitHub Actions, que nao tem D:\ nem C:\ da
# Jaqueline). sincronizar_midia_nuvem.py e quem sobe os arquivos ANTES e
# grava os campos *_nuvem em cada job; aqui so baixa de volta pra um
# temporario na hora de publicar. No PC local, o caminho normal (Windows)
# quase sempre ja existe, entao nunca baixa nada — zero mudanca de
# comportamento local.
# --------------------------------------------------------------------------
_ARQUIVOS_BAIXADOS_TEMP = []  # limpos no fim do processamento de cada job


def _baixar_temp(url, sufixo):
    print(f"  baixando midia da nuvem ({url.split('/')[-1]})...")
    r = _checar(requests.get(url, timeout=1800, stream=True))
    destino = Path(tempfile.gettempdir()) / f"nuvem_{int(time.time())}_{random.randint(1000,9999)}{sufixo}"
    with open(destino, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    _ARQUIVOS_BAIXADOS_TEMP.append(destino)
    return destino


def _resolver_arquivo(caminho_local, url_nuvem, sufixo_padrao=".mp4"):
    """Devolve um Path local usavel. Prioridade: arquivo local se existir no
    disco de verdade; senao baixa da URL da nuvem (se tiver); senao erro
    claro. Nunca baixa se o arquivo local ja existe (evita trafego/tempo a
    toa no PC da Jaqueline, onde o caminho local sempre existe)."""
    if caminho_local and Path(caminho_local).exists():
        return Path(caminho_local)
    if url_nuvem:
        sufixo = Path(caminho_local).suffix if caminho_local else sufixo_padrao
        return _baixar_temp(url_nuvem, sufixo)
    raise FileNotFoundError(
        f"midia nao encontrada nem local ('{caminho_local}') nem na nuvem "
        f"(campo *_nuvem ausente) — rode sincronizar_midia_nuvem.py primeiro se estiver na nuvem."
    )


def _resolver_video(job):
    return _resolver_arquivo(job.get("video_path"), job.get("video_url_nuvem"), ".mp4")


def _resolver_imagem(job):
    return _resolver_arquivo(job.get("image_path"), job.get("image_url_nuvem"), ".jpg")


def _resolver_imagens(job):
    locais = job.get("image_paths") or []
    urls = job.get("image_urls_nuvem") or []
    if locais and all(Path(c).exists() for c in locais):
        return [Path(c) for c in locais]
    if urls:
        return [_baixar_temp(u, ".jpg") for u in urls]
    raise FileNotFoundError(
        "imagens do carrossel nao encontradas nem local nem na nuvem "
        "(campo image_urls_nuvem ausente) — rode sincronizar_midia_nuvem.py primeiro se estiver na nuvem."
    )


def _limpar_temporarios_baixados():
    while _ARQUIVOS_BAIXADOS_TEMP:
        p = _ARQUIVOS_BAIXADOS_TEMP.pop()
        p.unlink(missing_ok=True)


def _midia_disponivel(job):
    """Usado no planejamento adiantado (processar_lotes) pra decidir se um
    job ja tem midia pronta (local OU na nuvem) ou se ainda precisa esperar
    o arquivo ser gerado/sincronizado."""
    if job.get("image_urls_nuvem") or job.get("video_url_nuvem") or job.get("image_url_nuvem"):
        return True
    caminhos = job.get("image_paths") or [job.get("video_path") or job.get("image_path")]
    caminhos = [c for c in caminhos if c]
    return not caminhos or all(Path(c).exists() for c in caminhos)


# --------------------------------------------------------------------------
# thumbnail automatica — busca na pasta "Geradas" de cada canal em vez de
# exigir thumb_path digitado a mao no job (28/07/2026, a pedido da
# Jaqueline: "isso no script tbm ok - p nao bugar"). Prioridade: se tiver
# mais de uma opcao (Teste A/B/C), usa sempre "Teste A" por padrao (regra
# ja documentada no cabecalho deste arquivo) — a nao ser que o job passe
# thumb_path explicito, que sempre vence.
# --------------------------------------------------------------------------
PASTA_CANAL = {
    "metodoana": "MetodoANA",
    "sacrarium": "Sacrarium",
    "protocolo4d": "Protocolo4D",
}
RAIZ_ESTRUTURA = Path(
    r"C:\Users\Jaqueline\Documents\Projeto Renda Extra\pipeline_completa\pipeline\03_Estrutura_Canais"
)


def _buscar_thumbnail_auto(job):
    """Tenta achar a thumbnail sozinho na pasta Geradas do canal, casando
    pelo prefixo do video (S08, V3...) tirado do nome do arquivo de video.
    Devolve None se nao achar nada (comportamento normal pra shorts que
    ainda nao tem thumb gerada — nao e erro, so significa 'sem thumbnail
    ainda', igual sempre foi)."""
    video_path = job.get("video_path")
    canal_pasta = PASTA_CANAL.get(job.get("canal"))
    if not video_path or not canal_pasta:
        return None
    pasta = RAIZ_ESTRUTURA / canal_pasta / "04-Pack de Postagem" / "Thumbnails" / "Geradas"
    if not pasta.exists():
        return None

    nome_video = Path(video_path).stem  # ex: "S08_Noventa_por_cento_da_Parcela_e_Juro" ou "V03_Metodo_ANA"
    m = re.match(r"([SV])0*(\d+)", nome_video, re.IGNORECASE)
    if not m:
        return None
    letra, numero = m.group(1).upper(), m.group(2)
    # arquivos de short mantem zero a esquerda (S08_...), arquivos de longo
    # gerados pela fila MRD NAO mantem (V3 ... Teste A.jpeg, nao V03) — por
    # isso testamos os dois formatos de prefixo.
    prefixos_possiveis = [f"{letra}{numero.zfill(2)}", f"{letra}{numero}"]

    candidatos = [f for f in pasta.iterdir() if f.is_file()
                  and any(f.name.upper().startswith(p) for p in prefixos_possiveis)]
    if not candidatos:
        return None
    teste_a = [f for f in candidatos if "teste a" in f.name.lower()]
    if teste_a:
        return teste_a[0]
    return sorted(candidatos)[0]


# --------------------------------------------------------------------------
# tempo
# --------------------------------------------------------------------------
def _to_utc(publish_at):
    d = dt.datetime.fromisoformat(publish_at)
    if d.tzinfo is None:
        raise ValueError(f"publish_at '{publish_at}' precisa ter fuso explicito (ex: -03:00 pra BRT)")
    return d.astimezone(dt.timezone.utc)


def _to_unix(publish_at):
    return int(_to_utc(publish_at).timestamp())


# --------------------------------------------------------------------------
# fila de comentarios pendentes (generica, reaproveita a pasta do youtube_upload.py)
# --------------------------------------------------------------------------
def _pendentes_path(plataforma, canal):
    ytmod.PENDENTES_DIR.mkdir(exist_ok=True)
    return ytmod.PENDENTES_DIR / f"comentarios_{plataforma}_{canal}.json"


def _load_pendentes(plataforma, canal):
    p = _pendentes_path(plataforma, canal)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_pendentes(plataforma, canal, lista):
    _pendentes_path(plataforma, canal).write_text(json.dumps(lista, ensure_ascii=False, indent=2), encoding="utf-8")


def _guardar_comentario_pendente_fb(job, object_id, agendado):
    texto = job.get("first_comment")
    if not texto:
        return
    canal = job["canal"]
    if not agendado:
        try:
            _, token = _fb_creds(canal)
            requests.post(f"{GRAPH}/{object_id}/comments",
                          data={"access_token": token, "message": texto}, timeout=30).raise_for_status()
            print("  comentario postado na hora (post ja ficou publico imediatamente)")
            return
        except Exception as e:
            print(f"  [aviso] falha ao comentar na hora, indo pra fila de pendentes: {e}")
    fila = _load_pendentes("facebook", canal)
    fila.append({"object_id": object_id, "title": job.get("id"), "comment": texto,
                 "publish_at": job["publish_at"]})
    _save_pendentes("facebook", canal, fila)
    print("  comentario guardado na fila de pendentes do Facebook (sera postado quando passar do horario agendado)")


def _check_pendentes_facebook(canal):
    fila = _load_pendentes("facebook", canal)
    if not fila:
        print(f"Nenhum comentario pendente de Facebook pro canal '{canal}'.")
        return
    _, token = _fb_creds(canal)
    restantes = []
    agora = dt.datetime.now(dt.timezone.utc)
    for item in fila:
        alvo = _to_utc(item["publish_at"])
        if agora >= alvo:
            try:
                requests.post(f"{GRAPH}/{item['object_id']}/comments",
                              data={"access_token": token, "message": item["comment"]}, timeout=30).raise_for_status()
                print(f"  comentario postado em '{item['title']}' ({item['object_id']})")
            except Exception as e:
                print(f"  [aviso] falha ao comentar em {item['object_id']}: {e}")
                restantes.append(item)
        else:
            print(f"  '{item['title']}' ainda nao passou do horario agendado, continua na fila")
            restantes.append(item)
    _save_pendentes("facebook", canal, restantes)


def check_pendentes_tudo(canal):
    print(f"=== Checando pendentes de YouTube pro canal '{canal}' ===")
    creds = ytmod.get_credentials(canal)
    youtube = build("youtube", "v3", credentials=creds)
    ytmod.check_pendentes(youtube, canal)
    print(f"\n=== Checando pendentes de Facebook pro canal '{canal}' ===")
    _check_pendentes_facebook(canal)


# --------------------------------------------------------------------------
# YouTube (reaproveita youtube_upload.py)
# --------------------------------------------------------------------------
def _processar_youtube(job):
    creds = ytmod.get_credentials(job["canal"])
    youtube = build("youtube", "v3", credentials=creds)
    job_utc = dict(job)
    job_utc["publish_at"] = _to_utc(job["publish_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    job_utc["video_path"] = str(_resolver_video(job))
    if job.get("thumb_path") and not Path(job["thumb_path"]).exists() and job.get("thumb_url_nuvem"):
        job_utc["thumb_path"] = str(_baixar_temp(job["thumb_url_nuvem"], ".jpg"))
    elif not job.get("thumb_path"):
        auto = _buscar_thumbnail_auto(job)
        if auto:
            job_utc["thumb_path"] = str(auto)
            print(f"  thumbnail achada automaticamente: {auto.name}")
    video_id = ytmod.upload_video(youtube, job_utc, job["canal"])
    return {"video_id": video_id, "url": f"https://youtube.com/watch?v={video_id}"}


# --------------------------------------------------------------------------
# Facebook
# --------------------------------------------------------------------------
FB_CHUNK_BYTES = 4 * 1024 * 1024  # 4MB por pedaco no upload resumable


def _fb_upload_video_resumable(job):
    """Post de video normal (nao-Reels) — usado pros longos (tipo=video).
    ATUALIZADO 23/07/2026: o endpoint simples /page-id/videos (upload direto
    num POST so) dava erro 413 mesmo com arquivo recodificado pra ~235MB
    (confirmado ao vivo com os longos do Sacrarium V02/V03, 482s/8min,
    recodificados de 590MB pra ~4Mbps e AINDA ASSIM 413) — o limite pratico
    desse endpoint e mais baixo do que a documentacao da Meta sugere,
    independente do tamanho final do arquivo. Troquei pro PROTOCOLO
    RESUMABLE oficial da Meta pra video de Pagina (start/transfer/finish em
    pedacos de 4MB), o mesmo tipo de abordagem que ja funciona pros Reels
    (`_fb_publicar_reels`) — mas aqui e via POST comum no /page-id/videos
    com upload_phase, nao via rupload.facebook.com."""
    canal = job["canal"]
    page_id, token = _fb_creds(canal)
    unix_ts = _to_unix(job["publish_at"])
    agendar = unix_ts > int(time.time()) + 600

    video_path = _resolver_video(job)
    temporario = None
    if video_path.stat().st_size > FB_VIDEO_LIMITE_BYTES:
        print(f"  video grande ({video_path.stat().st_size // (1024*1024)}MB), recodificando pra "
              f"~{FB_BITRATE_VIDEO} antes de subir pro Facebook...")
        temporario = _reencode_video(video_path, FB_BITRATE_VIDEO, "fb_reencode")
        video_path = temporario

    try:
        file_size = video_path.stat().st_size
        print(f"  upload resumable ({file_size // (1024*1024)}MB, pedacos de {FB_CHUNK_BYTES // (1024*1024)}MB)...")

        start = _checar(requests.post(f"{GRAPH}/{page_id}/videos", data={
            "upload_phase": "start", "access_token": token, "file_size": str(file_size),
        }, timeout=60))
        r = start.json()
        video_id = r["video_id"]
        upload_session_id = r["upload_session_id"]
        start_offset = int(r["start_offset"])
        end_offset = int(r["end_offset"])

        with open(video_path, "rb") as f:
            while start_offset != end_offset:
                f.seek(start_offset)
                chunk = f.read(end_offset - start_offset)
                r = _checar(requests.post(f"{GRAPH}/{page_id}/videos", data={
                    "upload_phase": "transfer", "access_token": token,
                    "upload_session_id": upload_session_id, "start_offset": str(start_offset),
                }, files={"video_file_chunk": chunk}, timeout=300)).json()
                start_offset = int(r["start_offset"])
                end_offset = int(r["end_offset"])
                print(f"    {start_offset // (1024*1024)}MB / {file_size // (1024*1024)}MB enviados...")

        finish_data = {
            "upload_phase": "finish", "access_token": token,
            "upload_session_id": upload_session_id, "description": job.get("caption", ""),
        }
        if agendar:
            finish_data["published"] = "false"
            finish_data["scheduled_publish_time"] = str(unix_ts)
        else:
            finish_data["published"] = "true"
        _checar(requests.post(f"{GRAPH}/{page_id}/videos", data=finish_data, timeout=120))
    finally:
        if temporario:
            temporario.unlink(missing_ok=True)

    object_id = video_id
    _guardar_comentario_pendente_fb(job, object_id, agendar)
    return {"object_id": object_id, "agendado": agendar, "url": f"https://www.facebook.com/{object_id}"}


def _fb_publicar_reels(job):
    canal = job["canal"]
    page_id, token = _fb_creds(canal)
    unix_ts = _to_unix(job["publish_at"])
    agendar = unix_ts > int(time.time()) + 600

    start = _checar(requests.post(f"{GRAPH}/{page_id}/video_reels",
                     data={"upload_phase": "start", "access_token": token}, timeout=60))
    r = start.json()
    video_id = r["video_id"]
    upload_url = r["upload_url"]

    video_path = _resolver_video(job)
    file_size = video_path.stat().st_size
    with open(video_path, "rb") as f:
        _checar(requests.post(upload_url, headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        }, data=f, timeout=1800))

    finish_data = {
        "upload_phase": "finish",
        "video_id": video_id,
        "access_token": token,
        "description": job.get("caption", ""),
    }
    if agendar:
        finish_data["video_state"] = "SCHEDULED"
        finish_data["scheduled_publish_time"] = str(unix_ts)
    else:
        finish_data["video_state"] = "PUBLISHED"
    _checar(requests.post(f"{GRAPH}/{page_id}/video_reels", data=finish_data, timeout=120))

    _guardar_comentario_pendente_fb(job, video_id, agendar)
    return {"video_id": video_id, "agendado": agendar, "url": f"https://www.facebook.com/reel/{video_id}"}


def _fb_upload_foto(job):
    canal = job["canal"]
    page_id, token = _fb_creds(canal)
    unix_ts = _to_unix(job["publish_at"])
    agendar = unix_ts > int(time.time()) + 600
    data = {"access_token": token, "caption": job.get("caption", "")}
    if agendar:
        data["published"] = "false"
        data["scheduled_publish_time"] = str(unix_ts)
    else:
        data["published"] = "true"
    imagem = _resolver_imagem(job)
    with open(imagem, "rb") as f:
        resp = requests.post(f"{GRAPH}/{page_id}/photos", data=data, files={"source": f}, timeout=300)
    resp.raise_for_status()
    object_id = resp.json()["id"]
    _guardar_comentario_pendente_fb(job, object_id, agendar)
    return {"object_id": object_id, "agendado": agendar, "url": f"https://www.facebook.com/{object_id}"}


def _fb_upload_carrossel(job):
    """Post de varias fotos no Facebook (13/07/2026, primeira vez testado)
    — nao existe 'carrossel' nativo no Facebook, o equivalente e um post no
    feed com varias fotos anexadas (attached_media). Aceita upload direto
    de arquivo local (nao precisa hospedar em URL, diferente do Instagram)."""
    canal = job["canal"]
    page_id, token = _fb_creds(canal)
    unix_ts = _to_unix(job["publish_at"])
    agendar = unix_ts > int(time.time()) + 600

    media_fbids = []
    for caminho in _resolver_imagens(job):
        with open(caminho, "rb") as f:
            r = _checar(requests.post(f"{GRAPH}/{page_id}/photos",
                         data={"access_token": token, "published": "false"},
                         files={"source": f}, timeout=300))
        media_fbids.append(r.json()["id"])

    data = {
        "access_token": token,
        "message": job.get("caption", ""),
        "attached_media": json.dumps([{"media_fbid": mid} for mid in media_fbids]),
    }
    if agendar:
        data["published"] = "false"
        data["scheduled_publish_time"] = str(unix_ts)
    else:
        data["published"] = "true"
    r_post = _checar(requests.post(f"{GRAPH}/{page_id}/feed", data=data, timeout=60))
    post_id = r_post.json()["id"]
    _guardar_comentario_pendente_fb(job, post_id, agendar)
    return {"post_id": post_id, "agendado": agendar, "url": f"https://www.facebook.com/{post_id}"}


def _processar_facebook(job):
    tipo = job["tipo"]
    if tipo == "reels":
        return _fb_publicar_reels(job)
    if tipo == "video":
        return _fb_upload_video_resumable(job)
    if tipo == "foto":
        return _fb_upload_foto(job)
    if tipo == "carrossel":
        return _fb_upload_carrossel(job)
    raise ValueError(f"tipo '{tipo}' nao suportado pro Facebook")


# --------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------
# AVISO (13/07/2026): o upload resumable direto (POST pro upload_uri que a
# Meta devolve, com header offset/file_size) foi abandonado — dava 400
# "ProcessingFailedError, retriable: false" nos 3 canais, 100% das vezes,
# mesmo com headers identicos ao documentado oficialmente. Ver
# [[project_instagram_reels_quebrado_2026_07_13]] na memoria pro historico
# completo da investigacao. Solucao adotada: hospedar o video temporariamente
# no Supabase Storage (bucket publico) e passar a URL publica via video_url
# — isso ja passa da etapa que travava (cria o container), o processamento
# assincrono e que precisa ser observado de perto por um tempo.
def _supabase_creds():
    d = _read_token_file("supabase.txt")
    return d["SUPABASE_URL"], d["SUPABASE_SECRET_KEY"], d["SUPABASE_BUCKET"]


def _supabase_upload(caminho, content_type="video/mp4"):
    url, key, bucket = _supabase_creds()
    caminho = Path(caminho)
    nome = f"{int(time.time())}_{caminho.name}"
    with open(caminho, "rb") as f:
        _checar(requests.post(
            f"{url}/storage/v1/object/{bucket}/{nome}",
            headers={"Authorization": f"Bearer {key}", "apikey": key,
                     "Content-Type": content_type, "x-upsert": "true"},
            data=f, timeout=1800,
        ))
    return nome, f"{url}/storage/v1/object/public/{bucket}/{nome}"


def _supabase_delete(nome):
    url, key, bucket = _supabase_creds()
    try:
        requests.delete(f"{url}/storage/v1/object/{bucket}",
                         headers={"Authorization": f"Bearer {key}", "apikey": key,
                                  "Content-Type": "application/json"},
                         json={"prefixes": [nome]}, timeout=60)
    except Exception as e:
        print(f"  [aviso] falha ao apagar '{nome}' do Supabase (limpar manualmente depois): {e}")


FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
IG_BITRATE_VIDEO = "3500k"
IG_BITRATE_AUDIO = "128k"
FB_VIDEO_LIMITE_BYTES = 300 * 1024 * 1024  # acima disso, recodifica antes de subir (ver _fb_upload_video_simples)
FB_BITRATE_VIDEO = "4000k"


def _reencode_video(video_path, bitrate_kbps, prefixo):
    """Recodifica o video pra um bitrate mais baixo via ffmpeg, devolve um
    Path temporario que o chamador deve apagar depois de usar."""
    video_path = Path(video_path)
    saida = Path(tempfile.gettempdir()) / f"{prefixo}_{int(time.time())}_{video_path.stem}.mp4"
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-c:v", "libx264", "-profile:v", "high",
        "-b:v", bitrate_kbps, "-maxrate", bitrate_kbps, "-bufsize", "7000k",
        "-c:a", "aac", "-b:a", IG_BITRATE_AUDIO,
        "-movflags", "+faststart",
        str(saida),
    ]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou ao recodificar '{video_path.name}': {resultado.stderr[-1500:]}")
    return saida


def _reencode_para_instagram(video_path):
    """Causa raiz achada em 13/07/2026: os videos originais (~9.5Mbps) dao
    erro no processamento do Instagram (upload resumable direto: 400
    ProcessingFailedError; video_url do Facebook: erro 2207076 no
    processamento assincrono) — MESMO com token/headers/URL corretos.
    Recodificando pra ~3.5Mbps o mesmo arquivo publica sem erro (testado e
    confirmado ao vivo). Ver [[project_instagram_reels_quebrado_2026_07_13]].
    Devolve um Path temporario que o chamador deve apagar depois de usar."""
    return _reencode_video(video_path, IG_BITRATE_VIDEO, "ig_reencode")


def _ig_publicar_reels(job):
    canal = job["canal"]
    ig_id, token = _ig_creds(canal)
    video_path = _resolver_video(job)

    print("  recodificando video pra bitrate menor (exigencia do processamento do Instagram)...")
    video_reencodado = _reencode_para_instagram(video_path)
    try:
        print("  subindo video pro Supabase (hospedagem temporaria pra dar URL publica pro Instagram)...")
        nome_supabase, video_url = _supabase_upload(video_reencodado)
    finally:
        video_reencodado.unlink(missing_ok=True)

    try:
        r = _checar(requests.post(f"{GRAPH}/{ig_id}/media", data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": job.get("caption", ""),
            "access_token": token,
        }, timeout=60))
        container_id = r.json()["id"]

        for _ in range(60):
            status = _checar(requests.get(f"{GRAPH}/{container_id}",
                              params={"fields": "status_code,status", "access_token": token}, timeout=30)).json()
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise RuntimeError(f"Instagram processou o video com erro: {status}")
            time.sleep(10)
        else:
            raise RuntimeError("Instagram nao terminou de processar o video a tempo (timeout de 10min)")

        pub = _checar(requests.post(f"{GRAPH}/{ig_id}/media_publish",
                       data={"creation_id": container_id, "access_token": token}, timeout=60))
        media_id = pub.json()["id"]
    finally:
        _supabase_delete(nome_supabase)

    resultado = {"media_id": media_id, "url": f"https://www.instagram.com/reel/{media_id}"}
    first = job.get("first_comment")
    if first:
        try:
            _checar(requests.post(f"{GRAPH}/{media_id}/comments",
                    data={"access_token": token, "message": first}, timeout=30))
            resultado["comentario"] = "postado na hora"
        except Exception as e:
            resultado["comentario_erro"] = str(e)
    return resultado


def _ig_publicar_carrossel(job):
    """Carrossel do Instagram (13/07/2026, primeira vez testado). A API do
    Instagram nao tem agendamento nativo pra nada, carrossel incluso —
    tenta publicar na hora sempre."""
    canal = job["canal"]
    ig_id, token = _ig_creds(canal)
    imagens = _resolver_imagens(job)

    nomes_supabase = []
    try:
        children = []
        for img in imagens:
            print(f"  subindo '{img.name}' pro Supabase...")
            nome, url_publica = _supabase_upload(img, content_type="image/jpeg")
            nomes_supabase.append(nome)
            r = _checar(requests.post(f"{GRAPH}/{ig_id}/media", data={
                "image_url": url_publica,
                "is_carousel_item": "true",
                "access_token": token,
            }, timeout=60))
            children.append(r.json()["id"])

        r_parent = _checar(requests.post(f"{GRAPH}/{ig_id}/media", data={
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": job.get("caption", ""),
            "access_token": token,
        }, timeout=60))
        container_id = r_parent.json()["id"]

        for _ in range(30):
            status = _checar(requests.get(f"{GRAPH}/{container_id}",
                              params={"fields": "status_code,status", "access_token": token}, timeout=30)).json()
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise RuntimeError(f"Instagram processou o carrossel com erro: {status}")
            time.sleep(5)
        else:
            raise RuntimeError("Instagram nao terminou de processar o carrossel a tempo (timeout de 2.5min)")

        pub = _checar(requests.post(f"{GRAPH}/{ig_id}/media_publish",
                       data={"creation_id": container_id, "access_token": token}, timeout=60))
        media_id = pub.json()["id"]
    finally:
        for nome in nomes_supabase:
            _supabase_delete(nome)

    resultado = {"media_id": media_id, "url": f"https://www.instagram.com/p/{media_id}"}
    first = job.get("first_comment")
    if first:
        try:
            _checar(requests.post(f"{GRAPH}/{media_id}/comments",
                    data={"access_token": token, "message": first}, timeout=30))
            resultado["comentario"] = "postado na hora"
        except Exception as e:
            resultado["comentario_erro"] = str(e)
    return resultado


def _ig_publicar_foto_unica(job):
    """Post de foto unica no feed do Instagram (13/07/2026). Mesma logica do
    carrossel mas com 1 imagem so e sem is_carousel_item/media_type=CAROUSEL
    — cria o container direto com image_url e publica."""
    canal = job["canal"]
    ig_id, token = _ig_creds(canal)
    imagem = _resolver_imagem(job)

    print(f"  subindo '{imagem.name}' pro Supabase...")
    nome_supabase, url_publica = _supabase_upload(imagem, content_type="image/jpeg")
    try:
        r = _checar(requests.post(f"{GRAPH}/{ig_id}/media", data={
            "image_url": url_publica,
            "caption": job.get("caption", ""),
            "access_token": token,
        }, timeout=60))
        container_id = r.json()["id"]
        pub = _checar(requests.post(f"{GRAPH}/{ig_id}/media_publish",
                       data={"creation_id": container_id, "access_token": token}, timeout=60))
        media_id = pub.json()["id"]
    finally:
        _supabase_delete(nome_supabase)

    resultado = {"media_id": media_id, "url": f"https://www.instagram.com/p/{media_id}"}
    first = job.get("first_comment")
    if first:
        try:
            _checar(requests.post(f"{GRAPH}/{media_id}/comments",
                    data={"access_token": token, "message": first}, timeout=30))
            resultado["comentario"] = "postado na hora"
        except Exception as e:
            resultado["comentario_erro"] = str(e)
    return resultado


def _processar_instagram(job):
    tipo = job["tipo"]
    if tipo == "reels":
        return _ig_publicar_reels(job)
    if tipo == "carrossel":
        return _ig_publicar_carrossel(job)
    if tipo == "foto":
        return _ig_publicar_foto_unica(job)
    raise ValueError(f"tipo '{tipo}' de Instagram nao suportado")


# --------------------------------------------------------------------------
# historico global de postados (cruza arquivos de lote diferentes)
# --------------------------------------------------------------------------
# Cada lote JSON so sabe o proprio status ("pendente" -> "agendado"/"publicado").
# Isso nao protege contra reposter o MESMO video se ele aparecer de novo num
# lote NOVO (ex: lote regerado). O historico resolve isso: guarda uma entrada
# por (canal, plataforma, arquivo de video/imagem) assim que o post e feito de
# verdade, e todo lote roda so os jobs que AINDA NAO estao no historico.
#
# Pra forcar um repost de proposito: apagar a entrada correspondente do
# historico_postagens.json (ou o arquivo inteiro, se for um reset geral) antes
# de rodar o lote de novo. Sem isso, o job vem marcado como "pulado" e nao
# chama a API de novo.
def _load_historico():
    HISTORICO_PATH.parent.mkdir(exist_ok=True)
    if HISTORICO_PATH.exists():
        return json.loads(HISTORICO_PATH.read_text(encoding="utf-8"))
    return {}


def _save_historico(historico):
    HISTORICO_PATH.parent.mkdir(exist_ok=True)
    HISTORICO_PATH.write_text(json.dumps(historico, ensure_ascii=False, indent=2), encoding="utf-8")


def _dia(publish_at):
    """Data (fuso BRT) do publish_at — usada so pra AGRUPAR e ORDENAR jobs
    por dia (dia 13 inteiro, todos os canais, antes do dia 14, etc). NAO
    bloqueia nada: YouTube e Facebook agendam nativamente pra qualquer data
    futura na mesma rodada, sem esperar o calendario real chegar la."""
    return _to_utc(publish_at).astimezone(BRT).date()


def _caminho_midia(job):
    """Identificador de midia do job — video/imagem unica, ou (carrossel)
    a lista de imagens junta numa string. Usado tanto no historico quanto
    na checagem de arquivo disponivel no disco."""
    caminho = job.get("video_path") or job.get("image_path")
    if caminho:
        return caminho
    imagens = job.get("image_paths")
    if imagens:
        return "|".join(imagens)
    return ""


def _chave_historico(job):
    return f"{job['canal']}|{job['plataforma']}|{_caminho_midia(job)}"


def _registrar_historico(job):
    historico = _load_historico()
    historico[_chave_historico(job)] = {
        "id": job.get("id"),
        "canal": job["canal"],
        "plataforma": job["plataforma"],
        "publish_at": job["publish_at"],
        "postado_em_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resultado": job.get("resultado"),
    }
    _save_historico(historico)


# --------------------------------------------------------------------------
# loop principal do lote
# --------------------------------------------------------------------------
def _processar_job(job):
    print(f"\n--- {job.get('id', '?')} ({job['plataforma']}/{job['tipo']}) canal={job['canal']} ---")
    try:
        if job["plataforma"] == "youtube":
            resultado = _processar_youtube(job)
            job["status"] = "agendado"
        elif job["plataforma"] == "facebook":
            resultado = _processar_facebook(job)
            job["status"] = "agendado" if resultado.get("agendado") else "publicado"
        elif job["plataforma"] == "instagram":
            resultado = _processar_instagram(job)
            job["status"] = "publicado"
        else:
            raise ValueError(f"plataforma desconhecida: {job['plataforma']}")
        job["resultado"] = resultado
        print(f"  OK -> {resultado}")
        _registrar_historico(job)
    except Exception as e:
        job["status"] = f"erro: {e}"
        print(f"  [ERRO] {e}")
        print("  (marcado como 'erro' no arquivo — nao vai tentar de novo sozinho; corrija e rode o lote de novo)")
    finally:
        _limpar_temporarios_baixados()


def _esperar_delay(delay_min, delay_max):
    s = random.randint(delay_min, delay_max)
    print(f"  aguardando {s}s antes da proxima acao (simulando humano)...", flush=True)
    time.sleep(s)


ESPERA_MAXIMA_INSTAGRAM_SEGUNDOS = 20 * 60  # 20 min (decidido 23/07/2026, ver abaixo)


def _aguardar_horario(publish_at, espera_maxima=ESPERA_MAXIMA_INSTAGRAM_SEGUNDOS):
    """Espera ate a hora certa pra publicar no Instagram (a API nao tem
    agendamento nativo pra nada, nem reels nem carrossel nem foto — so
    publica na hora ou nunca).

    ATUALIZACAO (23/07/2026): antes disso, essa funcao dormia ate o horario
    real chegar, sem limite — se o job fosse pra daqui a horas ou dias, o
    script inteiro ficava travado esperando. A Jaqueline foi clara: "a
    prioridade e agendar, e nao ficar esperando a hora". Agora, se a espera
    for maior que `espera_maxima` (20 min por padrao), a funcao DESISTE e
    devolve False sem publicar — o job continua "pendente" no arquivo e o
    proprio agendador tenta de novo na proxima vez que rodar (por isso e
    importante rodar o script de novo perto do horario real de cada post).
    Devolve True quando realmente esperou e chegou a hora."""
    alvo = _to_utc(publish_at)
    jitter = random.randint(10, 60)
    alvo_com_folga = alvo + dt.timedelta(seconds=jitter)
    agora = dt.datetime.now(dt.timezone.utc)
    restante = (alvo_com_folga - agora).total_seconds()
    if restante > espera_maxima:
        print(f"  horario ainda esta a {int(restante / 60)} min de distancia (> {int(espera_maxima / 60)} min) "
              f"— pulando por agora, continua 'pendente'. Roda o script de novo mais perto da hora.", flush=True)
        return False
    while True:
        agora = dt.datetime.now(dt.timezone.utc)
        restante = (alvo_com_folga - agora).total_seconds()
        if restante <= 0:
            return True
        dormir = min(restante, 300)
        print(f"  esperando o horario de publicar (faltam ~{int(restante / 60)} min)...", flush=True)
        time.sleep(dormir)


def _yt_token_ja_valido(canal):
    """Checagem SEM BLOQUEAR: tenta carregar o token do canal e, se estiver
    expirado, tenta so o refresh (chamada rapida, nao pede nada pra
    Jaqueline). Devolve True se ja esta pronto pra usar sem autorizacao
    nova, False se vai precisar do fluxo interativo (flow.run_local_server,
    que ai sim bloqueia esperando ela clicar 'Permitir' no navegador)."""
    token_path = ytmod.TOKENS_DIR / f"youtube_token_{canal}.json"
    if not token_path.exists():
        return False
    try:
        creds = ytmod.Credentials.from_authorized_user_file(str(token_path), ytmod.SCOPES)
    except Exception:
        return False
    if creds and creds.valid:
        return True
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(ytmod.Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return True
        except Exception:
            return False
    return False


def _verificar_autorizacoes_youtube(canais):
    """ATUALIZADO 23/07/2026 a pedido da Jaqueline: "antes de rodar cada
    canal, ele verifica todos que precisam, e ja me passa, assim eu posso
    rodar, autorizar e ai sair e deixar" — ou seja, ela quer ver de uma vez
    so QUANTOS e QUAIS canais vao pedir autorizacao nova, sem ser
    surpreendida um de cada vez no meio da publicacao de verdade.

    Duas passadas: a PRIMEIRA so confere (sem bloquear em nada — token
    valido ou refresh automatico, nenhum dos dois pede nada pra ela) e
    monta a lista de quem realmente precisa de autorizacao nova. A SEGUNDA
    so entao pede autorizacao, um de cada vez, e SO pros que precisam —
    mas agora ela ja sabe de antemao quantos sao antes de ficar esperando."""
    print(f"{'#' * 60}\nChecando autorizacao do YouTube pra {len(canais)} canal(is)...\n{'#' * 60}")
    precisam = []
    for canal in canais:
        ok = _yt_token_ja_valido(canal)
        print(f"  {canal}: {'ja autorizado' if ok else 'PRECISA autorizar de novo'}")
        if not ok:
            precisam.append(canal)

    if not precisam:
        print("\nTodos os canais ja estao autorizados. Comecando a processar os jobs de verdade.\n")
        return

    print(f"\n{len(precisam)} canal(is) precisam de autorizacao nova: {', '.join(precisam)}")
    print("Vou pedir uma de cada vez agora — fica por perto so ate terminar essas, "
          "depois pode sair e deixar rodando.\n")
    for canal in precisam:
        print(f"--- autorizando {canal} ---")
        ytmod.get_credentials(canal)  # bloqueia so aqui, esperando ela clicar "Permitir"
        print(f"  OK, {canal} autorizado.")
    print(f"\n{'#' * 60}\nTodos os canais autorizados. Comecando a processar os jobs de verdade.\n{'#' * 60}\n")


def processar_lotes(caminhos, delay_min, delay_max, so_plataformas=None):
    """Junta os jobs de TODOS os arquivos passados (pode ser so 1) e
    processa em DUAS ETAPAS SEPARADAS (corrigido em 13/07/2026):

    ETAPA 1 — YouTube + Facebook de TODOS os dias/canais, em ordem
    cronologica, sem esperar nada (agendamento nativo aceita qualquer data
    futura na mesma chamada de API).

    ETAPA 2 — SO DEPOIS, Instagram de TODOS os dias/canais, em ordem
    cronologica: publica na hora o que ja pode, espera (dormindo) o que
    ainda nao chegou no horario.

    Antes disso, cada dia processava as duas etapas juntas (nativo do dia X,
    depois Instagram do dia X esperando o horario real, so entao dia X+1) —
    isso fazia o agendamento nativo de um dia futuro (ex: dia 15) ficar
    refem da espera do Instagram de um dia anterior (ex: dia 14), que podia
    levar horas. Ver [[feedback_cadencia_postagem]] pro historico completo.

    `so_plataformas` (28/07/2026): filtro opcional (set de strings, ex:
    {"instagram"}) — usado pelo GitHub Actions, que so tem credenciais de
    Instagram+Supabase (YouTube/Facebook agendam nativo com UMA chamada so,
    entao rodam local mesmo, nao precisam de nuvem nem de token la). Jobs
    de outras plataformas ficam intocados (continuam 'pendente' no arquivo,
    pra rodar local depois)."""
    arquivos = {}  # Path -> dict completo do arquivo (pra salvar de volta)
    todos = []  # lista de (job, jobs_path)
    for caminho in caminhos:
        jobs_path = Path(caminho)
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        arquivos[jobs_path] = data
        for job in data["jobs"]:
            if so_plataformas and job["plataforma"] not in so_plataformas:
                continue
            todos.append((job, jobs_path))

    def salvar(jobs_path):
        jobs_path.write_text(json.dumps(arquivos[jobs_path], ensure_ascii=False, indent=2), encoding="utf-8")

    # Sincroniza mídia nova pro Supabase automaticamente ao rodar LOCAL
    # (28/07/2026, a pedido da Jaqueline: "um .bat a menos pra rodar"). So
    # roda isso local — na nuvem (AGENDADOR_TOKENS_DIR setado pelo workflow)
    # nao faz sentido, nao tem D:\ pra sincronizar de la mesmo, e o Supabase
    # creds la nem sao lidos do jeito que sincronizar_midia_nuvem.py espera.
    if "AGENDADOR_TOKENS_DIR" not in os.environ:
        try:
            url_sb, key_sb, bucket_sb = syncmod._supabase_creds()
            syncmod._garantir_bucket(url_sb, key_sb, bucket_sb)
            for jobs_path in arquivos:
                syncmod.sincronizar_arquivo(jobs_path, url_sb, key_sb, bucket_sb)
            # relê os arquivos do disco (sincronizar_arquivo escreve neles
            # direto) pra pegar os campos *_nuvem novos dentro de 'todos'.
            arquivos = {}
            todos = []
            for caminho in caminhos:
                jobs_path = Path(caminho)
                data = json.loads(jobs_path.read_text(encoding="utf-8"))
                arquivos[jobs_path] = data
                for job in data["jobs"]:
                    if so_plataformas and job["plataforma"] not in so_plataformas:
                        continue
                    todos.append((job, jobs_path))
            print()
        except Exception as e:
            print(f"[aviso] falha ao sincronizar midia com o Supabase (seguindo sem isso): {e}\n")

    canais = sorted({job["canal"] for job, _ in todos})

    # Checa autorizacao do YouTube de TODOS os canais logo de cara, antes de
    # publicar qualquer coisa — ver docstring de _verificar_autorizacoes_youtube.
    canais_youtube = sorted({job["canal"] for job, _ in todos if job["plataforma"] == "youtube"})
    if canais_youtube:
        _verificar_autorizacoes_youtube(canais_youtube)

    # Antes de agendar qualquer coisa nova, confere sozinho se algum
    # comentario fixado de um post de um DIA ANTERIOR (qualquer canal
    # envolvido) ja pode ser postado — sem precisar rodar --check-pendentes
    # na mao toda vez. Pulado inteiro no modo so_plataformas={"instagram"}
    # (GitHub Actions) — essa checagem e so de YouTube/Facebook, que nao tem
    # credencial la (de proposito, ver docstring), entao so geraria erro a
    # toa em todo ciclo do cron.
    if not so_plataformas or ({"youtube", "facebook"} & so_plataformas):
        for canal in canais:
            try:
                check_pendentes_tudo(canal)
            except Exception as e:
                print(f"[aviso] falha ao checar comentarios pendentes do canal '{canal}': {e}")
        print()

    historico = _load_historico()
    ja_feitos = 0
    pulados_historico = 0
    pendentes = []
    for job, jobs_path in todos:
        if job.get("status", "pendente") != "pendente":
            ja_feitos += 1
            continue
        if _chave_historico(job) in historico:
            info = historico[_chave_historico(job)]
            job["status"] = f"pulado: ja esta no historico (postado em {info['postado_em_utc']})"
            salvar(jobs_path)
            pulados_historico += 1
            continue
        pendentes.append((job, jobs_path))

    if ja_feitos:
        print(f"[aviso] {ja_feitos} job(s) ja tinham sido processados antes (arquivo) — pulando.")
    if pulados_historico:
        print(f"[aviso] {pulados_historico} job(s) ja estao no historico global — pulando "
              f"(pra forcar repost, apague a entrada em {HISTORICO_PATH.name}).")

    if not pendentes:
        print("\nNada pra fazer agora (ja foi tudo executado ou ja estava no historico).")
        return

    # Planejamento adiantado: se o video/imagem ainda nao existe no disco
    # (ex: exportando aos poucos no CapCut ao longo do mes), NAO e erro —
    # so fica esperando, continua "pendente", tenta de novo na proxima vez
    # que o script rodar.
    disponiveis = []
    aguardando_arquivo = []
    for job, jp in pendentes:
        if _midia_disponivel(job):
            disponiveis.append((job, jp))
        else:
            aguardando_arquivo.append((job, jp))

    if aguardando_arquivo:
        print(f"[aviso] {len(aguardando_arquivo)} job(s) aguardando arquivo de midia no disco "
              f"— continuam 'pendente':")
        for job, _jp in aguardando_arquivo:
            print(f"   - {job.get('id', '?')} ({_dia(job['publish_at']).isoformat()}): "
                  f"esperando {_caminho_midia(job)}")

    # ORDEM DE CANAL (decidido 23/07/2026, a pedido da Jaqueline): dentro do
    # mesmo dia/horario, processa sempre P4D -> MetodoANA -> Sacrarium ->
    # ForjandoTitas, nessa ordem — nao e so por organizacao: canais
    # diferentes usam tokens diferentes (cada canal tem seu proprio
    # facebook_<conta>.txt/instagram_<conta>.txt/youtube_token_<canal>.json),
    # entao alternar de canal a cada chamada ja alterna de token natural-
    # mente, o que ajuda a nao repetir o mesmo token em sequencia.
    ORDEM_CANAIS = {"protocolo4d": 0, "metodoana": 1, "sacrarium": 2, "forjandotitas": 3}

    def _ordem(job):
        return (_dia(job["publish_at"]), ORDEM_CANAIS.get(job["canal"], 99), job["publish_at"])

    nativos = [(j, jp) for j, jp in disponiveis if j["plataforma"] in ("youtube", "facebook")]
    espera = [(j, jp) for j, jp in disponiveis if j["plataforma"] == "instagram"]

    if nativos:
        nativos.sort(key=lambda x: _ordem(x[0]))
        print(f"\n{'#' * 60}\nETAPA 1 — agendando {len(nativos)} job(s) nativo(s) (YouTube/Facebook), "
              f"todos os dias e canais, ordem P4D->MetodoANA->Sacrarium->ForjandoTitas\n{'#' * 60}")
        dia_atual = None
        for i, (job, jp) in enumerate(nativos):
            d = _dia(job["publish_at"])
            if d != dia_atual:
                dia_atual = d
                print(f"\n--- dia {d.isoformat()} ---")
            _processar_job(job)
            salvar(jp)
            # So espera se a PROXIMA chamada repete o MESMO TOKEN (mesma
            # plataforma E mesmo canal — canais diferentes ja usam
            # credenciais diferentes, entao trocar de canal ja e suficiente
            # pra nao repetir token, mesmo mantendo a mesma plataforma).
            proximo = nativos[i + 1][0] if i < len(nativos) - 1 else None
            if proximo and proximo["plataforma"] == job["plataforma"] and proximo["canal"] == job["canal"]:
                _esperar_delay(delay_min, delay_max)

    if espera:
        espera.sort(key=lambda x: _ordem(x[0]))
        print(f"\n{'#' * 60}\nETAPA 2 — {len(espera)} job(s) de Instagram (sem agendamento nativo — "
              f"a API da Meta so publica na hora ou nunca, nao existe 'agendar' de verdade pro Insta), "
              f"ordem P4D->MetodoANA->Sacrarium, teto de espera de {ESPERA_MAXIMA_INSTAGRAM_SEGUNDOS // 60} min "
              f"por job\n{'#' * 60}")
        pulados_por_hora = 0
        for i, (job, jp) in enumerate(espera):
            chegou_a_hora = _aguardar_horario(job["publish_at"])
            if not chegou_a_hora:
                pulados_por_hora += 1
                continue
            _processar_job(job)
            salvar(jp)
            proximo = espera[i + 1][0] if i < len(espera) - 1 else None
            if proximo and proximo["canal"] == job["canal"]:
                _esperar_delay(delay_min, delay_max)
        if pulados_por_hora:
            print(f"\n[aviso] {pulados_por_hora} job(s) de Instagram continuam 'pendente' — horario "
                  f"ainda longe demais. Roda o script de novo mais perto da hora de cada um.")

    print("\nTodos os jobs disponiveis foram processados (o que ainda estiver 'pendente' "
          "esta esperando arquivo de midia no disco, ou esperando a hora certa do Instagram).")


def _parse_escolhas(texto, total):
    """Aceita '2' (um so), '1,2,3' ou '1 2 3' (varios, separados), ou '123'
    (varios colados, um digito por lote — so funciona enquanto tiver ate 9
    lotes na pasta). Retorna lista de indices (1-based), na ordem digitada."""
    texto = texto.strip()
    if not texto:
        return []
    partes = [p for p in re.split(r"[,\s]+", texto) if p]
    if len(partes) == 1 and len(partes[0]) > 1 and partes[0].isdigit():
        partes = list(partes[0])
    indices = []
    for p in partes:
        try:
            n = int(p)
        except ValueError:
            continue
        if 1 <= n <= total:
            indices.append(n)
    return indices


def _escolher_arquivos_interativo():
    """Lista os lotes em _agendamentos/. ATUALIZADO 23/07/2026 a pedido da
    Jaqueline: lotes com TODOS os jobs ja resolvidos (nenhum 'pendente' —
    seja porque foi publicado, seja porque deu erro) somem da lista, pra nao
    poluir o menu com coisa que ja acabou. Exibicao tambem ficou mais clara:
    nome do lote em destaque, contagem alinhada, arquivo em segundo plano."""
    JOBS_DIR.mkdir(exist_ok=True)
    todos_arquivos = sorted(JOBS_DIR.glob("*.json"))
    if not todos_arquivos:
        print(f"Nenhum lote encontrado em {JOBS_DIR}.")
        print(r"Passe o caminho direto: py agendador_publicacoes.py caminho\lote.json")
        return []

    candidatos = []  # (arquivo, nome_lote, n_pend, total)
    concluidos = 0
    for a in todos_arquivos:
        try:
            d = json.loads(a.read_text(encoding="utf-8"))
            jobs = d.get("jobs", [])
            n_pend = sum(1 for j in jobs if j.get("status", "pendente") == "pendente")
            nome_lote = d.get("lote", a.stem)
        except Exception:
            candidatos.append((a, a.stem, None, None))
            continue
        if n_pend == 0 and jobs:
            concluidos += 1
            continue
        candidatos.append((a, nome_lote, n_pend, len(jobs)))

    if not candidatos:
        print(f"Todos os {concluidos} lote(s) em {JOBS_DIR} ja estao concluidos (nenhum job pendente).")
        print(r"Passe o caminho direto se quiser rodar algum de novo mesmo assim: py agendador_publicacoes.py caminho\lote.json")
        return []

    print(f"Lotes com job pendente ({len(candidatos)} de {len(todos_arquivos)}"
          f"{f', {concluidos} ja concluido(s) escondido(s)' if concluidos else ''}):\n")
    for i, (a, nome_lote, n_pend, total) in enumerate(candidatos, 1):
        contagem = f"{n_pend}/{total} pendentes" if total is not None else "erro ao ler"
        print(f"  {i}. {nome_lote}")
        print(f"     {contagem} — {a.name}")
    escolha = input("\nQuais numeros rodar? (um so: 2 | varios: 123 ou 1,2,3) ").strip()
    indices = _parse_escolhas(escolha, len(candidatos))
    if not indices:
        print("Escolha invalida.")
        return []
    return [str(candidatos[i - 1][0]) for i in indices]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs_or_flag", nargs="?")
    ap.add_argument("canal", nargs="?")
    ap.add_argument("--check-pendentes", action="store_true",
                     help="so confere/posta comentarios pendentes (YouTube+Facebook), nao roda lote novo")
    ap.add_argument("--todos", action="store_true",
                     help="NUVEM (28/07/2026): roda TODOS os lotes pendentes de _agendamentos/ "
                          "automaticamente, sem perguntar nada (modo nao-interativo, pra cron/"
                          "GitHub Actions — o menu que pede input trava pra sempre num terminal sem "
                          "ninguem digitando). Ignora a pasta _backup_*.")
    ap.add_argument("--so-instagram", action="store_true",
                     help="NUVEM (28/07/2026): so processa jobs de plataforma=instagram, ignora "
                          "youtube/facebook por completo (nem checa credencial deles) — usado pelo "
                          "GitHub Actions, que so tem token de Instagram+Supabase. YouTube/Facebook "
                          "continuam rodando local (agendamento nativo, uma chamada so resolve).")
    ap.add_argument("--sem-instagram", action="store_true",
                     help="NUVEM (28/07/2026): espelho do --so-instagram, pro uso LOCAL — processa "
                          "youtube/facebook normalmente mas ignora instagram por completo (deixa "
                          "'pendente' pra nuvem cuidar sozinha). Evita rodar o mesmo job de "
                          "Instagram nos dois lugares (local + nuvem), que arriscaria postar em "
                          "duplicidade se os dois rodassem por perto um do outro.")
    ap.add_argument("--delay-min", type=int, default=20, help="segundos, padrao 20 (atualizado 23/07/2026 — era 240)")
    ap.add_argument("--delay-max", type=int, default=60, help="segundos, padrao 60 (atualizado 23/07/2026 — era 540)")
    args = ap.parse_args()

    if args.check_pendentes:
        canal = args.jobs_or_flag
        if not canal:
            print("Uso: py agendador_publicacoes.py --check-pendentes <canal>")
            return
        check_pendentes_tudo(canal)
        return

    if args.so_instagram:
        so_plataformas = {"instagram"}
    elif args.sem_instagram:
        so_plataformas = {"youtube", "facebook"}
    else:
        so_plataformas = None

    if args.todos:
        JOBS_DIR.mkdir(exist_ok=True)
        caminhos = [str(p) for p in sorted(JOBS_DIR.glob("*.json"))]
        if not caminhos:
            print(f"Nenhum lote encontrado em {JOBS_DIR}.")
            return
        print(f"Modo --todos: {len(caminhos)} arquivo(s) de lote encontrados, processando todos.")
        processar_lotes(caminhos, args.delay_min, args.delay_max, so_plataformas=so_plataformas)
        return

    jobs_path = args.jobs_or_flag
    if jobs_path:
        caminhos = [jobs_path]
    else:
        caminhos = _escolher_arquivos_interativo()
        if not caminhos:
            return

    processar_lotes(caminhos, args.delay_min, args.delay_max, so_plataformas=so_plataformas)


if __name__ == "__main__":
    main()
