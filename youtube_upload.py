"""
Upload e agendamento de vídeos no YouTube via YouTube Data API v3.

Uso:
    py youtube_upload.py jobs.json <canal>              # sobe e agenda os vídeos do lote
    py youtube_upload.py --check-pendentes <canal>       # posta comentários de vídeos que já ficaram públicos

<canal>: identificador curto do canal (ex: metodoana, sacrarium, protocolo4d) —
usado pra manter um token OAuth separado por canal (cada canal do YouTube
autoriza separadamente, mesmo usando o mesmo app/client_secret) e uma fila de
comentários pendentes separada por canal.

jobs.json: lista de objetos, cada um com:
    video_path, thumb_path, title, description, tags (lista), category_id,
    publish_at (ISO 8601 UTC, ex: "2026-07-09T15:00:00Z"), pinned_comment (opcional),
    language (opcional, padrao "pt" — define idioma do video e do audio),
    contains_synthetic_media (opcional, padrao True — disclosure de conteudo com IA),
    embeddable (opcional, padrao False — permitir incorporacao em outros sites),
    playlists (opcional — lista de chaves de playlist dentro de youtube_playlists_<canal>.json,
    criadas com 01_Scripts/criar_playlist.py; um video pode entrar em varias, ex:
    ["shorts", "consignado"] — formato + tema ao mesmo tempo. "playlist" no singular
    (uma chave so) tambem funciona, por compatibilidade)

OBS: "permitir remixagem" (Shorts remix) NAO tem campo na API do YouTube ainda —
so da pra configurar manualmente no Studio, video por video.

Primeira execução por canal: abre o navegador pra autorizar (login Google,
escolher o canal certo na tela de consentimento). Depois disso, salva
youtube_token_<canal>.json e reautentica sozinho nas próximas vezes.

REGRA DE COMENTÁRIO (decidido em 2026-07-08): vídeo agendado fica "private" até
a hora marcada, e a API do YouTube não deixa comentar em vídeo privado. Por
isso o script NUNCA tenta comentar na hora do upload — só guarda o comentário
numa fila (_pendentes/comentarios_<canal>.json). Rodar --check-pendentes no
INÍCIO de toda sessão de postagem seguinte: ele confere se algum vídeo da fila
já ficou público e posta o comentário nele (removendo da fila depois).

REGRA DE INTERVALO ENTRE UPLOADS: subir vários vídeos em sequência rápida pode
parecer atividade de bot pro sistema antifraude do YouTube. Intervalo padrão
entre uploads: 5 a 10 minutos (aleatório), configurável via --delay-min/--delay-max
(em segundos) se precisar rodar mais rápido em teste.
"""
import sys
import json
import time
import random
import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]

import os

TOKENS_DIR = Path(os.environ.get("AGENDADOR_TOKENS_DIR", r"C:\Users\Jaqueline\.claude\tokens"))
CLIENT_SECRET_PATH_FILE = TOKENS_DIR / "youtube_oauth_path.txt"
PENDENTES_DIR = Path(__file__).parent / "_pendentes"


def get_credentials(channel):
    """NUVEM (28/07/2026): so le o CLIENT_SECRET_PATH_FILE se de fato
    precisar do fluxo interativo (token ausente/revogado) — no GitHub
    Actions esse arquivo nao existe (nao faz sentido ter fluxo interativo
    la), mas isso nunca deveria travar um job com token/refresh_token
    validos, que e o caso normal em producao."""
    token_path = TOKENS_DIR / f"youtube_token_{channel}.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        refrescou = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refrescou = True
            except Exception as e:
                # refresh_token pode ter sido revogado/expirado de verdade
                # (nao so o access_token de curta duracao) — nesse caso o
                # refresh() lanca excecao em vez de simplesmente falhar, e
                # sem esse except a autenticacao trava aqui pra sempre em
                # vez de cair pro fluxo interativo abaixo.
                print(f">>> [aviso] refresh do token de '{channel}' falhou ({e}) — "
                      f"token provavelmente revogado, pedindo autorizacao nova.", flush=True)
        if not refrescou:
            client_secret_path = Path(CLIENT_SECRET_PATH_FILE.read_text(encoding="utf-8-sig").strip())
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
            print(f">>> Abra este link no navegador pra autorizar o canal '{channel}':", flush=True)
            creds = flow.run_local_server(port=0, open_browser=False)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f">>> Autorizado com sucesso, token salvo em {token_path.name}.", flush=True)
    return creds


def _pendentes_path(channel):
    PENDENTES_DIR.mkdir(exist_ok=True)
    return PENDENTES_DIR / f"comentarios_{channel}.json"


def _load_pendentes(channel):
    p = _pendentes_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_pendentes(channel, lista):
    _pendentes_path(channel).write_text(json.dumps(lista, ensure_ascii=False, indent=2), encoding="utf-8")


def _playlists_map_path(channel):
    return TOKENS_DIR / f"youtube_playlists_{channel}.json"


def _load_playlists_map(channel):
    p = _playlists_map_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def add_to_playlist(youtube, channel, playlist_key, video_id):
    mapa = _load_playlists_map(channel)
    playlist_id = mapa.get(playlist_key)
    if not playlist_id:
        print(f"  [aviso] chave de playlist '{playlist_key}' nao encontrada em youtube_playlists_{channel}.json — pulando")
        return
    youtube.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist_id,
                           "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
    ).execute()
    print(f"  adicionado a playlist '{playlist_key}' ({playlist_id})")


def upload_video(youtube, job, channel):
    idioma = job.get("language", "pt")
    body = {
        "snippet": {
            "title": job["title"],
            "description": job["description"],
            "tags": job.get("tags", []),
            "categoryId": str(job.get("category_id", 27)),
            "defaultLanguage": idioma,
            "defaultAudioLanguage": idioma,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": job["publish_at"],
            "selfDeclaredMadeForKids": False,
            # Disclosure de conteudo alterado/sintetico (2026-07-08): quase todo video
            # dessa pipeline usa voz/imagem gerada por IA, entao o padrao e True.
            # Passar "contains_synthetic_media": false no job so no caso raro de um
            # video sem nenhum elemento de IA.
            "containsSyntheticMedia": job.get("contains_synthetic_media", True),
            # Padrao do canal (2026-07-08, pedido da Jaqueline): incorporacao e
            # remixagem desligadas por padrao. Passar "embeddable": true no job pra
            # liberar excecao especifica.
            "embeddable": job.get("embeddable", False),
        },
    }
    media = MediaFileUpload(job["video_path"], chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  upload {int(status.progress() * 100)}%")
    video_id = response["id"]
    print(f"  OK video_id={video_id}  (agendado pra {job['publish_at']})")

    playlist_keys = job.get("playlists") or ([job["playlist"]] if job.get("playlist") else [])
    for playlist_key in playlist_keys:
        try:
            add_to_playlist(youtube, channel, playlist_key, video_id)
        except Exception as e:
            print(f"  [aviso] falha ao adicionar na playlist '{playlist_key}': {e}")

    thumb_path = job.get("thumb_path")
    if thumb_path and Path(thumb_path).exists():
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
            print(f"  thumbnail definida: {thumb_path}")
        except Exception as e:
            print(f"  [aviso] falha ao definir thumbnail (provavel canal sem verificacao de telefone "
                  f"habilitada em youtube.com/verify - setar manual no Studio): {e}")

    # Comentário NUNCA é tentado agora (vídeo está private/agendado) — só guarda na fila.
    pinned = job.get("pinned_comment")
    if pinned:
        fila = _load_pendentes(channel)
        fila.append({"video_id": video_id, "title": job["title"], "comment": pinned})
        _save_pendentes(channel, fila)
        print("  comentario guardado na fila de pendentes (sera postado quando o video ficar publico)")

    return video_id


def check_pendentes(youtube, channel):
    fila = _load_pendentes(channel)
    if not fila:
        print(f"Nenhum comentario pendente pro canal '{channel}'.")
        return
    restantes = []
    for item in fila:
        vid = item["video_id"]
        try:
            resp = youtube.videos().list(part="status", id=vid).execute()
            items = resp.get("items", [])
            status = items[0]["status"]["privacyStatus"] if items else None
        except Exception as e:
            print(f"  [aviso] nao consegui checar status de {vid}: {e}")
            restantes.append(item)
            continue
        if status == "public":
            try:
                youtube.commentThreads().insert(
                    part="snippet",
                    body={"snippet": {"videoId": vid,
                                       "topLevelComment": {"snippet": {"textOriginal": item["comment"]}}}},
                ).execute()
                print(f"  comentario postado em '{item['title']}' ({vid}) — FIXAR MANUALMENTE no Studio")
            except Exception as e:
                print(f"  [aviso] video publico mas falha ao comentar em {vid}: {e}")
                restantes.append(item)
        else:
            print(f"  '{item['title']}' ({vid}) ainda nao esta publico (status={status}), continua na fila")
            restantes.append(item)
    _save_pendentes(channel, restantes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs_or_flag", nargs="?")
    ap.add_argument("channel", nargs="?")
    ap.add_argument("--check-pendentes", action="store_true",
                     help="so confere/posta comentarios pendentes, nao sobe video novo")
    ap.add_argument("--delay-min", type=int, default=300, help="segundos, padrao 300 (5 min)")
    ap.add_argument("--delay-max", type=int, default=600, help="segundos, padrao 600 (10 min)")
    args = ap.parse_args()

    if args.check_pendentes:
        channel = args.jobs_or_flag
        if not channel:
            print("Uso: py youtube_upload.py --check-pendentes <canal>")
            return
        creds = get_credentials(channel)
        youtube = build("youtube", "v3", credentials=creds)
        check_pendentes(youtube, channel)
        return

    if not args.jobs_or_flag or not args.channel:
        print("Uso: py youtube_upload.py jobs.json <canal>")
        return
    channel = args.channel
    creds = get_credentials(channel)
    youtube = build("youtube", "v3", credentials=creds)
    jobs_path = Path(args.jobs_or_flag)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))

    results = []
    for i, job in enumerate(jobs):
        print(f"\n=== {job['title']} ===")
        try:
            vid = upload_video(youtube, job, channel)
            results.append({"title": job["title"], "video_id": vid,
                             "url": f"https://youtube.com/watch?v={vid}",
                             "publish_at": job["publish_at"]})
        except Exception as e:
            print(f"  [ERRO] falha nesse video, seguindo pro proximo: {e}", flush=True)
            results.append({"title": job["title"], "error": str(e)})
        if i < len(jobs) - 1:
            wait_s = random.randint(args.delay_min, args.delay_max)
            print(f"  aguardando {wait_s}s antes do proximo upload (evitar padrao de bot)...", flush=True)
            time.sleep(wait_s)

    out_path = jobs_path.with_name(jobs_path.stem + f"_resultado_{channel}.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResultado salvo em {out_path}")


if __name__ == "__main__":
    main()
