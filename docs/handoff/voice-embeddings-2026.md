# Sibiu 2026 · handoff pentru diarizare și embeddings

Branch-ul `codex/experimental-transcript-processing` pornește de la Tasca `main` la `a12672a`. Commitul `5480fa7` adaugă extragerea pe ferestre, modul `sliding-notes` și verificarea prezenței și a voturilor pe dovezi din transcriere. Procesul-verbal oficial servește la evaluare. Comenzile pentru experiment sunt în [scriptul de transcriere](../../scripts/council_transcript_experiment.py).

## Starea rulării locale

Lotul este în curs pe laptopul lui Alex. Inventarul are 19 ședințe oficiale, aproximativ 15 ore de audio. La predare, ședința din 13 ianuarie era salvată cu 37 de segmente și 16 vectori, iar cea din 29 ianuarie, de peste două ore, se procesa pe blocuri de zece minute. Primele șapte blocuri erau salvate și începuse blocul opt din 13.

- Progres live în `runtime/sibiu-2026-local/progress.log`. Erorile procesului sunt în `errors.log`, iar rezultatele pe ședință în `status.json`.
- Checkpointurile sunt în `runtime/sibiu-2026-local/blocks/`. Se refolosesc când înregistrarea are același SHA-256 și configurația blocului este aceeași. Fișierele complete sunt salvate separat pe ședință.
- Arhiva finală `runtime/sibiu-2026-local/sibiu-2026-local.zip` apare după parcurgerea lotului. Verifică `status.json` pentru toate cele 19 ID-uri înainte să consideri lotul complet. Arhiva conține inventarul, rezultatele per ședință și `auto-embeddings.jsonl`.
- Pe Git sunt publicate codul, inventarul și reperele pilotului. Audio-ul, vectorii reali, modelele, logurile și checkpointurile se află în directorul local de lucru.

## Reluare

Folosește Python 3.12 și rulează din rădăcina repo-ului. Pe laptopul curent, `..\runtime` este directorul pe care îl folosește deja procesul. Pornește comanda de reluare după ce procesul curent s-a oprit, pentru a evita doi scriitori în aceleași checkpointuri.

```powershell
python -m pip install -r scripts/voice_embeddings/requirements.txt
python -X utf8 -u scripts/voice_embeddings/run_local.py --runtime-dir ..\runtime
```

În directorul indicat, runnerul așteaptă:

- `sibiu-colab-retry-inputs/<meeting_id>.mp4` pentru fiecare rând din [inventarul ședințelor](../../scripts/voice_embeddings/meetings-colab.csv). Numele celor două ședințe din 29 mai includ ora.
- `models/diarization/titanet_large.onnx` și `models/diarization/segmentation.onnx`, aceleași modele pe care le instalează `setup-models`. Runnerul le verifică SHA-256 înainte de extracție.
- Opțional, `sibiu-colab-retry-inputs/reference-embeddings.jsonl` pentru sugestii de nume. Fără el, runnerul exportă vectori și grupuri anonime. Folosește scriptul [embeddings.py](../../scripts/voice_embeddings/embeddings.py) pentru a valida fișierul de referință.

Pentru a reface referințele pilotului, folosește `seed-clips.jsonl` cu copia YouTube originală din 27 aprilie. Copia Vimeo are altă proveniență și are nevoie de verificarea alinierii înainte de a reutiliza timpii.

```powershell
python scripts/voice_embeddings/embeddings.py extract ..\runtime\models\diarization\titanet_large.onnx scripts/voice_embeddings/seed-clips.jsonl sibiu-2026-04-27 ..\runtime\downloads\sedinta.webm ..\runtime\sibiu-colab-retry-inputs\reference-embeddings.jsonl
python scripts/voice_embeddings/embeddings.py validate ..\runtime\sibiu-colab-retry-inputs\reference-embeddings.jsonl
```

Colab a procesat 13 ianuarie dintr-un fișier încărcat. Descărcarea directă din Vimeo a dat 401 pentru restul, iar rularea din Drive a afișat doar începutul ședinței lungi din 29 ianuarie. Rularea activă folosește fișierele locale, pyannote pentru delimitarea vocilor și TitaNet ONNX pe CPU pentru vectori, fără Qwen.

## Interpretarea rezultatelor

- Pilotul a folosit 15 mostre pentru patru profiluri. Pe 26 de fragmente etichetate din ședința din 30 aprilie, 17 au primit numele corect, 9 voci fără profil au rămas fără nume și nu au apărut atribuiri greșite în acel eșantion. Pentru al patrulea profil lipsea o intervenție confirmată în setul de test.
- Rularea automată integrală a aceleiași ședințe a confirmat trei nume. Varianta direct din audio a produs 34 de grupuri, dintre care 22 fără nume, și cel puțin o atribuire greșită punctual. Acesta este motivul pentru care exportul anual păstrează scorul și marja drept sugestii de verificat.
- Pentru blocuri de zece minute, `B001-S1` și `B002-S1` sunt ID-uri locale blocului. Timpii rămân relativi la începutul înregistrării. La agregarea rezultatelor, verifică manual vocile recurente între blocuri și ședințe.
- Pașii de urmat după lot sunt validarea inventarului exportat, auditul pe ședințe ținute separat pentru test și calibrarea pragului și a marjei. Pentru minutare, următoarele experimente propuse sunt starea explicită a amendamentelor și voturilor reluate, recuperarea replicilor după repere din transcriere și rutarea ferestrelor dificile către un model mai puternic.
