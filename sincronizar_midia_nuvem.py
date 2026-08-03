"""
Sincroniza midia local (video/imagem) dos lotes pendentes pra um bucket
PERMANENTE do Supabase, e grava os campos *_nuvem em cada job — pra que o
agendador_publicacoes.py consiga publicar mesmo rodando num lugar sem acesso
ao D:\\ da Jaqueline (ex: GitHub Actions).

Uso:
    py sincronizar_midia_nuvem.py
        Sincroniza TODOS os lotes pendentes em _agendamentos/ (ignora
        _backup_*). Idempotente: job que ja tem *_nuvem preenchido nao sobe
        de novo.

    py sincronizar_midia_nuvem.py caminho\\lote.json
        So esse arquivo.

--------------------------------------------------------------------------
QUANDO RODAR
--------------------------------------------------------------------------
Sempre que um lote novo for criado/editado localmente (com caminhos de
video_path/image_path/image_paths ainda so locais), rodar este script UMA
VEZ no PC da Jaqueline antes do GitHub Actions poder publicar esses jobs —
o workflow na nuvem sozinho nao tem como enxergar o D:\\ dela. Depois disso,
o lote fica com os dois caminhos (local + nuvem) e funciona tanto rodando
local quanto na nuvem.

--------------------------------------------------------------------------
BUCKET PERMANENTE (diferente do bucket efemero que o agendador ja usa)
--------------------------------------------------------------------------
O agendador_publicacoes.py ja usa um bucket do Supabase (SUPABASE_BUCKET em
supabase.txt) mas so pra hospedar temporariamente durante o upload pro
Instagram — ele APAGA o arquivo logo depois de publicar. Este script usa um
bucket SEPARADO e PERMANENTE (nao apaga sozinho), pra guardar os arquivos
disponiveis o tempo que o job ainda estiver pendente. Nome do bucket: campo
opcional SUPABASE_BUCKET_PERMANENTE em supabase.txt (senao usa
"pipeline-midia" por padrao) — o script cria o bucket automaticamente na
primeira vez, se ainda nao existir.

Limpeza: depois que um job e marcado como "publicado"/"agendado" de verdade
(o proprio agendador_publicacoes.py faz isso), rode
`py sincronizar_midia_nuvem.py --limpar-publicados` pra apagar do bucket
permanente os arquivos de jobs que ja foram concluidos, e nao pagar storage
a toa.
"""
import sys
import json
import time
import tempfile
import argparse
import subprocess
from pathlib import Path

import requests

TOKENS_DIR = Path(r"C:\Users\Jaqueline\.claude\tokens")
JOBS_DIR = Path(__file__).parent / "_agendamentos"
BUCKET_PADRAO = "pipeline-midia"

# Video original (~9-10Mbps) estoura o limite de 50MB do plano free do
# Supabase (28/07/2026, achado ao vivo: erro 413 "Payload too large" tentando
# subir um short cru de ~90MB). Recodifica pro mesmo bitrate que ja e usado
# pro upload do Instagram (~3.5Mbps, da ~30MB pra um short de 70s) antes de
# subir pro bucket permanente — mesma logica de agendador_publicacoes.py.
import os
FFMPEG = os.environ.get("AGENDADOR_FFMPEG", r"C:\ffmpeg\bin\ffmpeg.exe")
BITRATE_VIDEO = "3500k"
BITRATE_AUDIO = "128k"
LIMITE_SUPABASE_FREE_BYTES = 45 * 1024 * 1024  # margem de seguranca abaixo dos 50MB reais


def _reencode_se_precisar(video_path):
    """Devolve o Path a usar no upload — o original se ja for pequeno, ou
    um temporario recodificado (que o chamador deve apagar depois) se for
    grande demais pro limite do bucket. None em caso de falha do ffmpeg
    (chamador decide o que fazer)."""
    video_path = Path(video_path)
    if video_path.stat().st_size <= LIMITE_SUPABASE_FREE_BYTES:
        return video_path, None
    saida = Path(tempfile.gettempdir()) / f"sync_reencode_{int(time.time())}_{video_path.stem}.mp4"
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-c:v", "libx264", "-profile:v", "high",
        "-b:v", BITRATE_VIDEO, "-maxrate", BITRATE_VIDEO, "-bufsize", "7000k",
        "-c:a", "aac", "-b:a", BITRATE_AUDIO,
        "-movflags", "+faststart",
        str(saida),
    ]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou ao recodificar '{video_path.name}': {resultado.stderr[-1000:]}")
    return saida, saida


def _checar(resp):
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} {resp.reason} em {resp.url} -> {resp.text[:1500]}")
    return resp


def _supabase_creds():
    path = TOKENS_DIR / "supabase.txt"
    d = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    bucket = d.get("SUPABASE_BUCKET_PERMANENTE", BUCKET_PADRAO)
    return d["SUPABASE_URL"], d["SUPABASE_SECRET_KEY"], bucket


def _garantir_bucket(url, key, bucket):
    r = requests.get(f"{url}/storage/v1/bucket/{bucket}",
                      headers={"Authorization": f"Bearer {key}", "apikey": key}, timeout=30)
    if r.status_code == 200:
        return
    print(f"  bucket '{bucket}' nao existe ainda, criando (publico)...")
    _checar(requests.post(f"{url}/storage/v1/bucket",
                           headers={"Authorization": f"Bearer {key}", "apikey": key,
                                    "Content-Type": "application/json"},
                           json={"id": bucket, "name": bucket, "public": True}, timeout=30))
    print(f"  bucket '{bucket}' criado.")


def _content_type(caminho):
    ext = Path(caminho).suffix.lower()
    return {
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    }.get(ext, "application/octet-stream")


def _caminho_remoto(job, campo, caminho_local):
    """Organiza por canal/plataforma/nome-do-arquivo — facilita achar/limpar
    manualmente pelo painel do Supabase se precisar."""
    nome = Path(caminho_local).name
    return f"{job['canal']}/{job['plataforma']}/{job.get('id', 'job')}_{campo}_{nome}"


def _upload_se_preciso(url, key, bucket, caminho_local, caminho_remoto):
    temporario = None
    caminho_upload = Path(caminho_local)
    if caminho_upload.suffix.lower() in (".mp4", ".mov"):
        caminho_upload, temporario = _reencode_se_precisar(caminho_upload)
        if temporario:
            print(f"    (recodificado: {temporario.stat().st_size // (1024*1024)}MB, era "
                  f"{Path(caminho_local).stat().st_size // (1024*1024)}MB)")
    try:
        with open(caminho_upload, "rb") as f:
            _checar(requests.post(
                f"{url}/storage/v1/object/{bucket}/{caminho_remoto}",
                headers={"Authorization": f"Bearer {key}", "apikey": key,
                         "Content-Type": _content_type(caminho_local), "x-upsert": "true"},
                data=f, timeout=1800,
            ))
    finally:
        if temporario:
            temporario.unlink(missing_ok=True)
    return f"{url}/storage/v1/object/public/{bucket}/{caminho_remoto}"


def _apagar(url, key, bucket, caminho_remoto):
    try:
        requests.delete(f"{url}/storage/v1/object/{bucket}",
                         headers={"Authorization": f"Bearer {key}", "apikey": key,
                                  "Content-Type": "application/json"},
                         json={"prefixes": [caminho_remoto]}, timeout=60)
    except Exception as e:
        print(f"  [aviso] falha ao apagar '{caminho_remoto}': {e}")


def sincronizar_arquivo(jobs_path, url, key, bucket):
    """Cada job e tentado de forma ISOLADA (28/07/2026: achado ao vivo que
    um erro num job — ex: video grande demais — travava a funcao inteira
    com excecao nao tratada, e nenhum job SEGUINTE do mesmo arquivo (outros
    canais/plataformas) chegava a ser sincronizado). Um erro agora so pula
    aquele job especifico e continua com os outros."""
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    mudou = False
    for job in data.get("jobs", []):
        if job.get("status", "pendente") != "pendente":
            continue  # ja publicado/agendado/erro — nao precisa mais na nuvem

        # So o Instagram de fato precisa de uma copia publica no Supabase (a
        # API da Meta so aceita video_url/image_url, nao aceita upload direto
        # de arquivo local). YouTube e Facebook publicam com upload direto do
        # arquivo local (rodam so no PC da Jaqueline mesmo, nunca no GitHub
        # Actions — ver comentario em processar_lotes() no agendador), entao
        # nao ha motivo pra recodificar/subir esses videos pro bucket
        # permanente: e so gasto de tempo/banda e, pros longos, ainda estoura
        # o limite de tamanho do Supabase (03/08/2026, achado ao vivo com
        # varios longos de Cathedra Petri/Forjando Titas/Na Propria Pele).
        if job.get("plataforma") != "instagram":
            continue

        try:
            if job.get("video_path") and Path(job["video_path"]).exists() and not job.get("video_url_nuvem"):
                print(f"  [{job.get('id')}] subindo video...")
                remoto = _caminho_remoto(job, "video", job["video_path"])
                job["video_url_nuvem"] = _upload_se_preciso(url, key, bucket, job["video_path"], remoto)
                job["video_url_nuvem_remoto"] = remoto  # guardado so pra --limpar-publicados achar depois
                mudou = True
        except Exception as e:
            print(f"  [ERRO] {job.get('id')} (video): {e} — pulando esse job, continuando com os outros")

        try:
            if job.get("image_path") and Path(job["image_path"]).exists() and not job.get("image_url_nuvem"):
                print(f"  [{job.get('id')}] subindo imagem...")
                remoto = _caminho_remoto(job, "imagem", job["image_path"])
                job["image_url_nuvem"] = _upload_se_preciso(url, key, bucket, job["image_path"], remoto)
                job["image_url_nuvem_remoto"] = remoto
                mudou = True
        except Exception as e:
            print(f"  [ERRO] {job.get('id')} (imagem): {e} — pulando esse job, continuando com os outros")

        try:
            if job.get("image_paths") and not job.get("image_urls_nuvem"):
                existentes = [c for c in job["image_paths"] if Path(c).exists()]
                if len(existentes) == len(job["image_paths"]):
                    print(f"  [{job.get('id')}] subindo {len(existentes)} imagem(ns) do carrossel...")
                    urls, remotos = [], []
                    for i, caminho in enumerate(job["image_paths"]):
                        remoto = _caminho_remoto(job, f"img{i}", caminho)
                        urls.append(_upload_se_preciso(url, key, bucket, caminho, remoto))
                        remotos.append(remoto)
                    job["image_urls_nuvem"] = urls
                    job["image_urls_nuvem_remoto"] = remotos
                    mudou = True
        except Exception as e:
            print(f"  [ERRO] {job.get('id')} (carrossel): {e} — pulando esse job, continuando com os outros")

        try:
            if job.get("thumb_path") and Path(job["thumb_path"]).exists() and not job.get("thumb_url_nuvem"):
                print(f"  [{job.get('id')}] subindo thumbnail...")
                remoto = _caminho_remoto(job, "thumb", job["thumb_path"])
                job["thumb_url_nuvem"] = _upload_se_preciso(url, key, bucket, job["thumb_path"], remoto)
                job["thumb_url_nuvem_remoto"] = remoto
                mudou = True
        except Exception as e:
            print(f"  [ERRO] {job.get('id')} (thumb): {e} — pulando esse job, continuando com os outros")

    if mudou:
        jobs_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {jobs_path.name}: atualizado com URLs da nuvem.")
    else:
        print(f"  {jobs_path.name}: nada novo pra sincronizar.")


def limpar_publicados(jobs_paths, url, key, bucket):
    for jobs_path in jobs_paths:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        mudou = False
        for job in data.get("jobs", []):
            if job.get("status", "pendente") == "pendente":
                continue
            for campo_url, campo_remoto in [
                ("video_url_nuvem", "video_url_nuvem_remoto"),
                ("image_url_nuvem", "image_url_nuvem_remoto"),
                ("thumb_url_nuvem", "thumb_url_nuvem_remoto"),
            ]:
                if job.get(campo_remoto):
                    _apagar(url, key, bucket, job[campo_remoto])
                    del job[campo_remoto]
                    del job[campo_url]
                    mudou = True
            if job.get("image_urls_nuvem_remoto"):
                for remoto in job["image_urls_nuvem_remoto"]:
                    _apagar(url, key, bucket, remoto)
                del job["image_urls_nuvem_remoto"]
                del job["image_urls_nuvem"]
                mudou = True
        if mudou:
            jobs_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  {jobs_path.name}: limpo (jobs concluidos apagados do bucket permanente).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs_path", nargs="?")
    ap.add_argument("--limpar-publicados", action="store_true",
                     help="apaga do bucket permanente os arquivos de jobs ja publicados/agendados/erro")
    args = ap.parse_args()

    url, key, bucket = _supabase_creds()
    _garantir_bucket(url, key, bucket)

    if args.jobs_path:
        arquivos = [Path(args.jobs_path)]
    else:
        JOBS_DIR.mkdir(exist_ok=True)
        arquivos = sorted(JOBS_DIR.glob("*.json"))

    if args.limpar_publicados:
        limpar_publicados(arquivos, url, key, bucket)
        return

    if not arquivos:
        print(f"Nenhum lote encontrado em {JOBS_DIR}.")
        return

    print(f"Sincronizando {len(arquivos)} arquivo(s) de lote com o bucket '{bucket}'...\n")
    for jobs_path in arquivos:
        print(f"--- {jobs_path.name} ---")
        sincronizar_arquivo(jobs_path, url, key, bucket)
    print("\nPronto. Os lotes ja tem os campos *_nuvem preenchidos — agora o GitHub "
          "Actions consegue publicar mesmo sem acesso ao D:\\.")


if __name__ == "__main__":
    main()
