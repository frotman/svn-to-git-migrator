# SVN → Git Migrator

Applicazione desktop Python per importare un ramo Subversion in Git, con autori, date e messaggi di commit. La versione **1.2.0** segue la storia della directory attraverso copie e spostamenti e importa soltanto le revisioni pertinenti fino al limite scelto.

## Requisiti

- Python **3.10+** con Tkinter.
- Client a riga di comando **Git** e **Subversion** (`svn`).
- Accesso ai repository tramite i client del sistema operativo.

Funziona con il motore SVN CLI + Git su Windows, Linux, macOS e WSL. Le nuove importazioni non richiedono `git-svn` né pacchetti Python da installare con `pip`.

## Avvio

Su Windows, esegui **AVVIA_WINDOWS.cmd**, oppure apri PowerShell nella cartella del progetto:

```powershell
python3.exe -X utf8 .\svn_git_migrator.py
```

Se usi il launcher Python, il comando equivalente è `py -3 .\svn_git_migrator.py`.

Su Linux o macOS:

```bash
python3 svn_git_migrator.py
```

Per verificare le dipendenze senza collegarti ai repository, aggiungi `--self-check`.

## Uso

Compila i quattro campi:

| Campo | Esempio |
| --- | --- |
| URL SVN del ramo | `https://svn.example.org/repo/branches/release` |
| Revisione massima | `5000` |
| URL del repository Git già creato | `https://git.example.org/team/project.git` |
| Nome del branch Git | `import-svn` |

Premi **Verifica**, poi **Importa / riprendi**. L'importazione prepara un repository locale; **Pubblica su Git** invia il branch solo quando lo richiedi. Un branch remoto con commit differenti rimane protetto.

La prima revisione è calcolata automaticamente. Un ramo creato a r5000 può ereditare una storia che inizia a r1200: l'interfaccia mostra separatamente origine della storia e creazione del percorso attuale.

## Configurazione e dati locali

Non serve configurare file prima dell'avvio. Dopo un'analisi riuscita, i quattro campi sono ricordati in:

- Windows: `%LOCALAPPDATA%\svn-git-migrator\settings.json`.
- Linux, macOS e WSL: `$XDG_CONFIG_HOME/svn-git-migrator/settings.json`, con ripiego su `~/.config/svn-git-migrator/settings.json`.

Le migrazioni sono salvate sotto `~/SVN_Git_Migrations/`, fuori dalla cartella del programma. Contengono repository importato, log, messaggi, autori e stato per la ripresa. Questi dati e `settings.json` possono identificare progetti privati e vanno tenuti separati dai sorgenti pubblici dello strumento. Il `.gitignore` incluso copre anche copie locali di questi file operativi.

Le credenziali restano gestite da Git, SVN e SSH. Su Windows viene usata normalmente anche la configurazione SVN in `%APPDATA%\Subversion`. L'app non chiede né conserva password e non carica automaticamente file `.env`.

Per installazioni personalizzate sono disponibili tre variabili d'ambiente facoltative: `SVNGIT_GIT_EXE`, `SVNGIT_SVN_EXE` e `SVNGIT_SVN_CONFIG_DIR`. Gli esempi e le istruzioni di autenticazione sono in [LEGGIMI.md](LEGGIMI.md).

## Ambito e sviluppo

L'importazione ricostruisce una storia lineare della directory scelta e dei suoi antenati. Non ricostruisce il grafo dei merge SVN; `svn:externals` è escluso. Autori SVN e riferimenti alle revisioni rimangono nei commit; le email `login@UUID` sono identificativi tecnici.

`svn_git_migrator.py` è il programma completo in un solo file. Per rigenerarlo dai sorgenti separati e creare lo ZIP:

```bash
python3 build_release.py
```

Per eseguire i test:

```bash
python3 -m unittest discover -s tests -v
```

Le integrazioni richiedono anche `svnadmin`; i soli test del motore precedente richiedono `git-svn`. Gli esiti e i limiti delle verifiche sono descritti in [VERIFICHE.txt](VERIFICHE.txt). La guida completa è [LEGGIMI.md](LEGGIMI.md).

## Licenza

Licenza da definire prima di presentare il progetto come open source.
