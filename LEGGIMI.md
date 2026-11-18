# SVN → Git — Importazione di un branch · 1.2.0

Applicazione Python con interfaccia grafica e quattro parametri. Importa la storia del ramo SVN scelto, seguendone i percorsi precedenti attraverso copie e spostamenti, fino a una revisione massima. Prepara un repository Git locale e permette di pubblicarlo su un branch Git dedicato.

## I quattro campi

| Campo | Cosa inserire |
| --- | --- |
| **SVN URL** | L'URL completo della directory del branch nel repository **Subversion originale**, per esempio `https://svn.example.org/repo/progetto/branches/release`. Una pagina web GitLab contenente `/-/commits/` non è un URL SVN. |
| **Revision max** | Un numero di revisione SVN, per esempio `3500000`. Il valore è un limite incluso. |
| **Git URL** | L'URL di un repository Git già creato e al quale puoi accedere, per esempio `git@example.org:gruppo/progetto.git`. |
| **Git branch name** | Il nome del branch da creare, per esempio `import-svn` o `migrazione/release`. |

La revisione iniziale viene calcolata automaticamente. Non occorre compilare un file degli autori o indicare la directory di lavoro.

### Come trovare l'URL SVN corretto

Da una copia di lavoro SVN già presente sul PC, apri PowerShell ed esegui:

```powershell
svn info --show-item url "C:\percorso\della\copia-svn"
```

Puoi anche copiarlo dal **Repository Browser di TortoiseSVN**, selezionando il ramo da importare. L'URL deve puntare alla directory del ramo che esisteva alla revisione scelta.

La pagina GitLab `https://git.example.org/gruppo/progetto/-/commits/nome-branch` mostra la cronologia Git. Non identifica il repository SVN di origine. Per il campo Git usa invece l'URL **Clone** del repository, per esempio `https://git.example.org/gruppo/progetto.git`; il nome del branch di destinazione va nel quarto campo.

L'app controlla le forme comuni delle pagine web GitLab e segnala il campo errato prima di collegarsi al server.

## Quale storia viene importata

**Dalla versione 1.2.0 l'app segue anche la storia ereditata dalle copie della directory del ramo.** Se il ramo nasce copiando un altro branch, o il trunk, la conversione risale quel percorso fino alla revisione usata dalla copia e continua a seguirne gli eventuali antenati. Include copie e spostamenti della directory scelta e delle directory che la contengono.

La selezione corrisponde alla storia della directory restituita da `svn log` senza `--stop-on-copy`. La revisione massima viene usata anche come **peg revision** (`URL@REV`): identifica il ramo esistente in quel momento, anche se il medesimo percorso è stato spostato, cancellato o riutilizzato in seguito. La revisione iniziale è la più antica trovata in questa storia; non è un numero fisso né una scansione di tutti i numeri SVN intermedi.

La finestra distingue **l'origine della storia importata** dalla **creazione del ramo corrente**. Per esempio, un ramo creato a r5000 può ereditare una storia iniziata a r1200. La versione 1.1.x si fermava all'ultima copia; la 1.2.0 include anche gli antenati pertinenti. La revisione più vecchia visualizzata in una pagina del client SVN potrebbe non essere l'inizio assoluto, a causa di paginazione o filtri. La verifica dell'app calcola l'inizio dal log disponibile al client, senza limite al numero di risultati.

Le revisioni che riguardano soltanto altri percorsi non diventano commit del branch importato. Anche le modifiche al ramo sorgente **successive alla revisione usata per la copia** sono escluse, se non fanno parte della storia selezionata. Non occorre importare tutti i branch antenati come branch Git separati.

I commit mantengono messaggio, data e identificativo dell'autore SVN. Per l'autore viene generata un'email del tipo `login@UUID-del-repository`: è un identificativo sintetico, non un indirizzo email personale verificato. Il riferimento alla revisione originale rimane nel messaggio come `git-svn-id`, con **l'URL storico relativo a quella revisione**, che può essere diverso dall'URL del ramo corrente.

Il motore nativo conserva in Git i contenuti canonici SVN: fine riga LF per i file con `svn:eol-style`, keyword SVN non espanse, link simbolici e bit eseguibile. Le regole `.gitignore` non eliminano file dall'importazione. I filtri esterni Git vengono disabilitati soltanto per il checkout conclusivo, senza modificare la configurazione globale del PC; eventuali regole di fine riga di `.gitattributes` possono comunque influenzare la copia di lavoro, non gli oggetti Git importati.

La conversione produce una storia Git lineare del ramo. Segue gli antenati della **directory scelta come radice**; non ricostruisce il grafo dei merge SVN e non aggiunge automaticamente tutta la storia precedente di ogni singolo file o sottodirectory copiati al suo interno in momenti successivi. Questi elementi vengono comunque importati nei contenuti del ramo a partire dalla loro aggiunta. Le proprietà SVN prive di un equivalente Git e le directory vuote non sono una copia integrale del modello SVN; i contenuti di `svn:externals` sono esclusi. I dati storici devono essere accessibili con le credenziali del client SVN.

## Requisiti

- Python **3.10 o successivo**, con Tkinter.
- Il client a riga di comando **Subversion** (`svn`).
- **Git**. Il motore delle nuove importazioni usa i client SVN e Git su **Windows, Linux, macOS e WSL**; non richiede `git svn`.
- Credenziali e accesso di rete ai due repository, configurati nell'ambiente in cui viene eseguita l'app.

Il programma usa la libreria standard di Python: non richiede pacchetti `pip`. Git e Subversion sono programmi esterni e devono essere installati.

`svn_git_migrator.py` è il programma completo in **un solo file**. Gli altri file Python nello ZIP sono i sorgenti separati e lo script di assemblaggio, utili soltanto per lo sviluppo; non servono per avviare il file principale.

### Windows: avvio nativo

1. Estrai tutto lo ZIP in una cartella.
2. Fai doppio clic su **AVVIA_WINDOWS.cmd**.
3. Compila i quattro campi e premi **Verifica**.

Sia la finestra sia i comandi Git/SVN vengono eseguiti **direttamente in Windows**. Il launcher cerca un Python Windows già installato con Tkinter, a partire da `py -3`; verifica anche `python.exe` e `python3.exe` quando si trovano in `WindowsApps`. Non avvia WSL e non installa programmi automaticamente. All'avvio mostra l'interprete selezionato.

Servono Python con Tcl/Tk, **Git for Windows** e il client **`svn.exe`**. Se usi già TortoiseSVN, il client a riga di comando è una sua componente opzionale: riapri l'installer, scegli **Modify/Modifica** e abilita **command line client tools** se `svn.exe` manca. `TortoiseProc.exe` da solo non sostituisce quel client.

L'app cerca Git e SVN nel `PATH` e nelle installazioni Windows comuni. Durante la verifica mostra gli eseguibili scelti. Non modifica il `PATH` del sistema. Se usi installazioni in cartelle personalizzate, avvia l'app da un terminale in cui i relativi comandi funzionano.

Per il controllo preliminare da PowerShell, nella cartella estratta:

```powershell
.\AVVIA_WINDOWS.cmd --self-check
```

Per verificare soltanto la finestra di Python:

```powershell
py -3 -m tkinter
```

Se sul tuo PC il comando disponibile è `python3.exe`, usa invece:

```powershell
python3.exe -m tkinter
```

L'opzione **`-3` appartiene al launcher `py`**: non va aggiunta a `python.exe` o `python3.exe`. Con quest'ultimo puoi anche eseguire direttamente il controllo e l'applicazione, dalla cartella estratta:

```powershell
python3.exe -X utf8 .\svn_git_migrator.py --self-check
python3.exe -X utf8 .\svn_git_migrator.py
```

Se il launcher segnala che Python o Tkinter mancano, usa [Python per Windows](https://www.python.org/downloads/windows/). L'installazione deve includere Tcl/Tk; nell'installer classico la voce è **Tcl/Tk and IDLE**.

Puoi anche avviare direttamente:

```powershell
py -3 svn_git_migrator.py
```

### WSL 2: alternativa separata

**AVVIA_WINDOWS_WSL.cmd** rimane disponibile per chi vuole usare gli strumenti Linux. Usa la distribuzione WSL predefinita e richiede WSLg per la finestra. Per il funzionamento nativo descritto sopra usa invece **AVVIA_WINDOWS.cmd**.

Nel terminale Ubuntu/WSL i requisiti sono:

```bash
sudo apt update
sudo apt install python3 python3-tk git subversion
python3 svn_git_migrator.py
```

Le credenziali e il lavoro di un'esecuzione WSL rimangono nell'ambiente Linux. Il launcher nativo non trasferisce automaticamente quei dati. Per l'installazione WSL è disponibile la [guida Microsoft](https://learn.microsoft.com/windows/wsl/install).

### Linux, Debian o Ubuntu

```bash
sudo apt update
sudo apt install python3 python3-tk git subversion
python3 svn_git_migrator.py
```

Serve una sessione grafica disponibile. Le altre distribuzioni usano nomi di pacchetto e gestori differenti.

### macOS

Installa Python con Tkinter, Subversion e Git. Verifica i tre comandi seguenti, poi avvia `python3 svn_git_migrator.py`. Il motore delle nuove importazioni non usa `git svn` né i relativi binding Perl.

### Controlli preliminari Linux/macOS/WSL

Esegui questi comandi nello stesso ambiente nel quale avvierai l'app:

```bash
python3 -m tkinter
svn --version --quiet
git --version
```

Il primo apre una finestra di prova. Per Windows nativo usa i controlli indicati nella sezione Windows.

Il controllo complessivo senza aprire l'interfaccia è:

```bash
python3 svn_git_migrator.py --self-check
```

Il codice del motore precedente `git svn` rimane nei sorgenti per compatibilità con le API e per le verifiche delle vecchie importazioni. Il suo uso esplicito richiede ancora `git svn`; non è il motore selezionato dalla GUI 1.2.0 e non è un requisito per le nuove migrazioni.

## Autenticazione

L'app non chiede né conserva password. Usa i client SVN/Git installati e le credenziali disponibili al processo. **Su Windows sfrutta gli strumenti e la configurazione Windows**, compresa la directory Subversion `%APPDATA%\Subversion`.

Le credenziali salvate da un client SVN nativo, come TortoiseSVN, possono essere riutilizzate dal client `svn.exe` quando usano la stessa configurazione e un formato di memorizzazione compatibile. Per Git viene usato il gestore credenziali già configurato. L'accesso effettuato soltanto nel browser o in un IDE non garantisce che anche i client a riga di comando siano autenticati.

La verifica dell'app non apre richieste interattive di password. Se manca l'accesso, configura prima l'autenticazione nella console Windows usando i comandi seguenti; non inserire password o token negli URL.

Per controllare SVN e permettere al client di gestire l'autenticazione in console:

```bash
svn info -r 3500000 "https://svn.example.org/repo/progetto/branches/release@3500000"
```

Per verificare l'accesso in lettura alla destinazione Git:

```bash
git ls-remote "git@example.org:gruppo/progetto.git"
```

La possibilità di leggere il repository non garantisce il permesso di pubblicare. Branch protetti e regole del server vengono applicati anche all'app.

### Installazioni o configurazioni personalizzate su Windows

L'app rileva automaticamente le installazioni comuni. Se il tuo ambiente usa percorsi differenti, puoi specificarli **soltanto per il terminale corrente**, senza aggiungere campi alla finestra:

```powershell
$env:SVNGIT_GIT_EXE = 'C:\strumenti\Git\cmd\git.exe'
$env:SVNGIT_SVN_EXE = 'C:\strumenti\Subversion\bin\svn.exe'
$env:SVNGIT_SVN_CONFIG_DIR = 'C:\configurazioni\Subversion'
.\AVVIA_WINDOWS.cmd
```

Sono esempi: imposta soltanto le variabili necessarie con i percorsi reali. La directory Subversion indicata deve essere quella già usata dal tuo client, se vuoi riutilizzarne l'accesso. L'app passa `--config-dir` a SVN; non legge né copia il contenuto delle password salvate.

**Windows e WSL usano ambienti e archivi credenziali distinti.** Se l'app gira in WSL, prepara l'accesso dal terminale WSL. Usa URL senza password o token incorporati. Per server con certificati privati, configura correttamente la fiducia del client; il programma non disabilita la verifica TLS.

## Configurazione locale e pubblicazione dei sorgenti

Non è necessario creare un file di configurazione per avviare il programma. I quattro campi della finestra vengono salvati in `settings.json` dopo un'analisi riuscita, fuori dalla cartella dell'applicazione:

| Ambiente | Percorso predefinito |
| --- | --- |
| Windows | `%LOCALAPPDATA%\svn-git-migrator\settings.json` |
| Linux, macOS e WSL | `$XDG_CONFIG_HOME/svn-git-migrator/settings.json`, oppure `~/.config/svn-git-migrator/settings.json` quando `XDG_CONFIG_HOME` non è impostata |

Il file contiene URL SVN, revisione massima, URL Git e nome del branch Git. Non contiene password, ma URL e nomi dei repository possono identificare progetti privati. Le cartelle di migrazione sotto `~/SVN_Git_Migrations/` contengono inoltre codice importato, autori, messaggi di commit, percorsi storici, log e stato delle operazioni.

Per pubblicare lo strumento, includi i sorgenti e la documentazione del programma. Mantieni separati `settings.json`, le cartelle di migrazione e gli archivi credenziali dei client. Il repository Git **prodotto dall'importazione** contiene i dati del progetto migrato e non fa parte dei sorgenti di questo strumento.

Il file `.gitignore` fornito esclude configurazioni e file operativi, cache Python, ambienti virtuali, archivi ZIP di build e impostazioni locali degli IDE. Non esclude genericamente tutti i JSON e conserva il file unico `svn_git_migrator.py` tra i file pubblicabili. Le esclusioni non rimuovono file già tracciati: prima di pubblicare, controlla i file preparati per il commit.

Le sole configurazioni facoltative degli eseguibili sono le variabili d'ambiente `SVNGIT_GIT_EXE`, `SVNGIT_SVN_EXE` e `SVNGIT_SVN_CONFIG_DIR`, descritte sopra. Non è previsto un file `.env` caricato automaticamente. Credenziali Git, cache di autenticazione SVN e chiavi SSH rimangono gestite dai rispettivi client.

## Uso

1. Compila i quattro campi.
2. Premi **Verifica** per controllare gli strumenti e analizzare la storia del percorso SVN e dei suoi antenati. Il riepilogo mostra la revisione iniziale calcolata, le revisioni pertinenti e il confine di creazione del ramo corrente.
3. Premi **Importa / riprendi** per creare o completare il repository Git locale. L'operazione può richiedere tempo; la finestra rimane utilizzabile e mostra avanzamento e log.
4. Al termine, controlla il riepilogo. Premi **Pubblica su Git** per inviare il risultato al branch indicato.

La verifica e l'importazione locale non pubblicano automaticamente su Git. La pubblicazione interessa soltanto il branch indicato. Se quel branch esiste già e punta a un commit diverso, l'app si ferma; se contiene già esattamente lo stesso commit, lo considera pubblicato. Per una nuova migrazione verso una destinazione già usata, scegli un nuovo nome di branch.

## Interruzione e ripresa

Il pulsante di arresto interrompe il processo in corso. I dati locali già importati vengono conservati; puoi riprendere usando gli stessi parametri.

Le importazioni vengono salvate automaticamente sotto `~/SVN_Git_Migrations/`, in una sottocartella identificata dalla configurazione. Su Windows si tratta normalmente di `C:\Users\TUO_UTENTE\SVN_Git_Migrations\`; in WSL `~` indica la home Linux. Il percorso effettivo è mostrato nell'app. Un'importazione iniziata in WSL non viene ripresa automaticamente dalla nuova directory Windows.

**Passaggio dalla 1.1.x alla 1.2.0:** l'identificativo della cartella di lavoro include ora l'ambito della storia (`history_scope=lineage`). Con gli stessi quattro campi, la nuova importazione usa una cartella distinta da quella della vecchia importazione limitata all'ultima copia. Le vecchie cartelle vengono conservate; i loro commit parziali non vengono mescolati alla nuova storia. Apri la 1.2.0, verifica gli stessi parametri e avvia l'importazione della storia completa della directory. Se avevi già pubblicato il risultato precedente, scegli **un nuovo nome di branch Git**: quello esistente rimane protetto e non viene riscritto.

Il pulsante **Apri cartella** apre quella posizione. Vi trovi `repository/`, il log completo `migration.log`, la lista `revisions.txt` e i file di stato. La finestra mantiene soltanto le righe di log più recenti per non consumare memoria su importazioni grandi. I quattro campi vengono ricordati dopo una verifica riuscita.

Non cancellare né modificare manualmente la directory di lavoro durante un'importazione. Se un processo viene arrestato brutalmente o il sistema si spegne, la ripresa può richiedere il controllo degli eventuali file di lock prima di proseguire.

## Prestazioni

L'app chiede al server SVN l'elenco delle revisioni della directory scelta, seguendo i suoi antenati. Esegue un checkout iniziale del percorso storico più antico e poi aggiorna la copia locale soltanto alle revisioni pertinenti. Quando la storia attraversa una copia o uno spostamento, passa al percorso storico corretto. Per ogni revisione legge i metadati locali SVN e inserisce gli oggetti dei file nuovi o modificati con `git fast-import`. Questo motore è usato su tutte le piattaforme.

Non viene esportato l'intero repository SVN e non si eseguono checkout o export per ciascuna revisione globale. Non si enumerano i numeri intermedi tra due revisioni pertinenti. Un repository con milioni di revisioni può quindi produrre un'importazione composta da un numero molto inferiore di commit, determinato dalla storia effettiva del ramo e dei suoi antenati.

La scansione dei metadati locali viene ripetuta per ciascuna revisione pertinente. Su rami con molti file anche questo costo incide. Occorre inoltre spazio per la working copy SVN e i suoi file pristine, per il repository Git e per lo stream temporaneo di una revisione. Il client SVN e il filesystem Windows devono poter rappresentare i nomi presenti nel ramo: collisioni di maiuscole/minuscole o nomi riservati a Windows possono impedire il checkout. Il repository Git locale abilita il supporto ai percorsi lunghi su Windows.

La lettura della storia dipende comunque dal server SVN. La conversione richiede accesso ai contenuti storici e lavoro su disco; il numero di revisioni globali, da solo, non permette di stimare la durata. I fattori principali restano il numero di commit da importare, la quantità di dati e file modificati, la latenza di rete e la velocità del server. La velocità rispetto ad altri strumenti va misurata sul repository effettivo; le verifiche incluse non costituiscono un benchmark di importazioni su reti aziendali.

## Problemi comuni

| Messaggio o situazione | Intervento |
| --- | --- |
| `git svn` non disponibile | Le nuove importazioni della 1.2.0 richiedono soltanto Git e SVN su tutte le piattaforme. Se il controllo continua a richiederlo, verifica di aver avviato il file della versione aggiornata. |
| Tkinter non disponibile o finestra impossibile da aprire | Su Windows verifica `py -3 -m tkinter` e il componente Tcl/Tk di Python. Su Ubuntu installa `python3-tk`; soltanto l'avvio WSL dipende da WSLg. |
| `svn.exe` non trovato su Windows | Abilita la componente **command line client tools** di TortoiseSVN o installa un client SVN Windows. |
| URL SVN riconosciuto come pagina GitLab | Inserisci l'URL del ramo nel repository SVN originale, ricavandolo dalla copia di lavoro o dal Repository Browser. |
| Autenticazione SVN/Git fallita | Prova i comandi di verifica in console e configura lì le credenziali. |
| Branch Git già presente con contenuto diverso | Scegli un nuovo nome di branch; il programma non sovrascrive quel branch. |
| URL SVN non disponibile alla revisione richiesta | Controlla URL e Revision max. La revisione deve contenere la directory del ramo. |
| Branch SVN cancellato dopo Revision max | Usa l'URL che identificava il ramo alla revisione scelta. L'app aggiunge una peg revision esplicita per leggere quella storia. |
| La revisione iniziale è molto precedente alla creazione del ramo corrente | È previsto: la 1.2.0 segue anche la storia dei percorsi da cui il ramo è stato copiato. Il riepilogo distingue le due revisioni. |
| Dopo l'aggiornamento compare una nuova cartella di lavoro | È previsto: la storia con gli antenati ha un'identità diversa dall'importazione parziale della 1.1.x. Le cartelle precedenti sono conservate. |
| Nessuna revisione trovata | Controlla che l'URL sia quello del ramo e che il limite sia corretto. |
| Errore del server durante la pubblicazione | La copia locale resta disponibile; risolvi permessi, branch protetti o limiti del server prima di riprovare. |

## Riferimenti

- [Documentazione Subversion: svn log](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.log.html)
- [Documentazione Subversion: peg revision e storia dei percorsi](https://svnbook.red-bean.com/en/1.8/svn.advanced.pegrevs.html)
- [Documentazione Git: git fast-import](https://git-scm.com/docs/git-fast-import)
- [Documentazione Git: git push](https://git-scm.com/docs/git-push)
- [Documentazione Python: Tkinter](https://docs.python.org/3/library/tkinter.html)
- [Python: uso su Windows](https://docs.python.org/3/using/windows.html)
- [Apache Subversion: client Windows](https://subversion.apache.org/packages.html#windows)
- [TortoiseSVN: download](https://tortoisesvn.net/downloads.html)
- [Microsoft: applicazioni grafiche Linux in WSL](https://learn.microsoft.com/windows/wsl/tutorials/gui-apps)

Le verifiche eseguite sono riportate in `VERIFICHE.txt` e usano repository SVN e Git locali di prova.
