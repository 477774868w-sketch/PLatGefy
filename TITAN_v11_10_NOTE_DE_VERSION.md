# TITAN PLAT v11.10 / TITAN STAKE v1.6 — note de version

**Moteur** 11.10.0 · **Modèle** 11.4.0 (inchangé) · **Mise** 1.6.0
Self-tests : moteur 44/44 (38 avant), mise 34/34 (28 avant), audit 89/89.

---

## 0. Le critère de réussite retenu, et pourquoi celui-là

> **Le nombre de courses que l'opérateur doit journaliser avant que le système
> puisse rendre un verdict falsifiable sur l'existence d'un avantage.**
> Plus il est bas, mieux c'est. Je l'appelle ici *courses-jusqu'au-verdict*.

Trois raisons de préférer ce critère à « la hiérarchie est-elle meilleure ».

**1. C'est le goulot d'étranglement réel, et la mission le dit.** Six versions,
neuf modules de gouvernance, 66 tests — et zéro course probante. Le système
n'est pas limité par sa finesse sportive, il est limité par le fait que rien
n'y est mesurable avant des mois. La mission tranche elle-même : « une
amélioration qui ne pourra pas être mesurée avant des mois vaut moins qu'un
outil qui accélère l'accumulation ».

**2. L'annexe rend tout ajout sportif improbable a priori.** Son §10 est sans
ambiguïté : presque tous les facteurs du domaine ont un A/E proche de 1. La
probabilité qu'une variable de plus produise un avantage est donc faible.
Quand la valeur espérée de chaque ajout est basse, **la capacité à mesurer vaut
plus que n'importe quel ajout particulier** — c'est elle qui permettra d'écarter
les quatre-vingt-dix-neuf idées mortes pour garder la centième.

**3. C'est le seul angle mort qu'un système ne peut pas se corriger seul.** Un
système qui ne se mesure pas finit par se convaincre qu'il marche. Six versions
de gouvernance impeccable et zéro mesure, c'est exactement la trajectoire d'un
système qui accumule de la cohérence sans jamais rencontrer la réalité.

### Ce que le critère donne, chiffré

Simulation appariée (`power2.py`, méthode en annexe), modèle détenant une part
de l'information que le marché du snapshot ignore encore :

| Avantage réel | Valeur de clôture (v1.6) | Test de calibration (existant) |
|---|---|---|
| 0,20 | **20 courses** | ~10 000 courses |
| 0,12 | **40 courses** | > 10 000 courses |
| 0,06 | **150 courses** | > 10 000 courses |

Le test de calibration lit des **arrivées**, dont la variance est énorme. La
valeur de clôture lit des **prix**, dont la variance est petite. C'est toute la
différence, et elle vaut deux à trois ordres de grandeur.

Avant : le premier verdict falsifiable arrivait à 200 courses — et la simulation
montre qu'il n'aurait probablement pas conclu même là.
Après : entre 20 et 150 courses selon la taille de l'effet, sans un euro engagé.

---

## 1. Les trois changements, et pourquoi chacun pourrait avoir tort

### 1.1 Valeur de clôture (STAKE v1.6) — l'apport principal

`clv` joint `TICKETS.jsonl` (qui enregistrait déjà les probabilités du modèle sur
tout le peloton, dès la première course papier) à `PRICES.jsonl`. Par course, en
coordonnées log-ratio centrées :

```
d = clr(p_modèle)  - clr(p_marché_snapshot)     notre désaccord
m = clr(p_clôture) - clr(p_marché_snapshot)     le mouvement propre du marché
β = Σ(d·m) / Σ(d·d)
```

β est la part de notre désaccord que le marché finit par adopter. Bootstrap
apparié par journée, comme partout ailleurs dans le module.

**Ce que β n'est pas.** En pari à cote fixe, battre la clôture *est* l'avantage.
En parimutuel, **on est payé le rapport final quel que soit le moment de la
mise** : le prix qu'on « prend » n'existe pas. β mesure donc si le modèle *sait*
quelque chose, jamais s'il *rapporte*. Le module le répète à chaque émission.

**Pourquoi cela pourrait avoir tort.**

- **Le biais que j'ai dû corriger, et qui est le point le plus important de
  cette version.** `d` et `m` partagent `p_marché_snapshot`. Tout bruit de pool
  dans ce vecteur entre dans les deux avec le même signe et **fabrique un β
  positif alors même que le modèle ne sait rien**. Reproduit en test : un modèle
  à information nulle contre un marché bruité rend β = 0,32, IC [0,29 ; 0,36] —
  un avantage inexistant, annoncé avec confiance. Pour ce projet, c'est le pire
  résultat possible. Corrigé par un estimateur à bases séparées utilisant le
  relevé T-10 que le protocole impose déjà : sur les mêmes données nulles il
  rend 0,0009, IC [-0,07 ; 0,06] ; sur un signal réel de 0,55 il retrouve 0,548.
  **Une lecture naïve du §5.8 de la mission aurait produit l'estimateur biaisé.**
- **β est un détecteur, pas un objectif.** Le maximiser reviendrait à rapprocher
  le modèle du marché — c'est-à-dire à détruire exactement ce qu'il mesure. Un
  modèle qui prédit parfaitement la clôture n'a aucun avantage sur elle : il la
  recopie. Ne jamais optimiser β.
- **La monnaie tardive est en grande partie algorithmique.** La littérature
  attribue les baisses de cote de dernière minute au *computer-assisted
  wagering*. Un β positif signifie donc « nous sommes d'accord avec les autres
  équipes informatiques » — information réelle, mais trade encombré, et en
  parimutuel notre propre mise déplacerait le prix contre nous.
- **Diffusion lente d'information publique.** Si le modèle et la monnaie tardive
  réagissent au même fait que le marché du snapshot n'avait pas encore intégré,
  β est positif sans aucune analyse supérieure.
- **Sélection.** Les courses que l'opérateur choisit d'analyser ne sont pas un
  échantillon aléatoire du programme.

**Gouvernance.** β n'ouvre aucune porte : ni niveau d'échelle, ni plafond, ni
calibration. Un test vérifie que l'échelle est identique avec et sans lui. La
calibration **autorise**, la valeur de clôture **mesure** — les confondre serait
ouvrir une porte avec un instrument.

### 1.2 Longueur de battue (moteur v11.10)

`within_race_performance()` ne lisait que la place et la taille du peloton : un
4e battu d'une encolure et un 4e battu de quinze longueurs avaient la même note.
`form_lines` accepte désormais `beaten_lengths` et `distance_m`, deux faits qui
se lisent sur la même ligne de résultat que la place. La marge est convertie en
perte **relative** de temps, ce qui rend cinq longueurs sur 1200 m plus coûteuses
que sur 2800 m sans avoir à tabuler quoi que ce soit.

Démontré en audit : sur des chevaux de même place, même allocation, même
peloton, la v11.9 rend 0,0 pour tous ; la v11.10 sépare +0,039 (battu de 0,2 L)
de −0,072 (battu de 20 L).

**Pourquoi cela pourrait avoir tort.**

- **C'est presque certainement un facteur à A/E ≈ 1.** La marge est publique et
  lue par tout le monde. Cela améliore la **hiérarchie sportive**, pas le
  rendement. Je ne revendique aucun gain de pari, et le seuil de comparaison
  reste le prélèvement de 15 %, pas zéro.
- **`MARGIN_WEIGHT = 0,5` est un cadran déclaré, non calibré.** Un cheval qu'on
  laisse finir sans insister affiche une marge qui exagère sa défaite ; le rang y
  est insensible. J'ai donc gardé les deux lectures à parts égales. Si les
  chevaux ménagés sont rares en France, 0,5 gaspille du signal ; s'ils sont
  fréquents, 0,5 est déjà trop.
- **La conversion ignore le rythme.** Cinq longueurs dans une course lente ne
  valent pas cinq longueurs dans une course rapide — c'est le point même de
  l'annexe sur le `finishing_speed_pct`. Une table de par piste-distance-terrain
  le corrigerait ; elle n'existe pas (voir §3).
- **Le plancher d'incertitude relatif est contournable.** Un collecteur qui ne
  déclare *aucune* marge n'est pas pénalisé, puisqu'il n'existe alors aucun
  différentiel. C'est délibéré — un plancher absolu serait insatisfaisable et
  divergerait — mais cela laisse une échappatoire : la paresse uniforme échappe
  à la règle, la paresse sélective non.

### 1.3 Effets humains en A/E (moteur v11.10) — un retrait, pas un ajout

`human_equipment` était un score libre plafonné à ±0,35 : le **dernier axe où un
chiffre pouvait entrer dans le hash sans qu'aucun fait ne l'adosse**. La règle
existait dans l'annexe (§3, §7), mais ni dans le moteur ni dans le prompt.

Une valeur non nulle exige désormais un `human_records` déclaré — périmètre,
fenêtre, dénominateur, victoires, attendu — et **le moteur calcule le score**.
Rétrécissement en n/(n+200) : un A/E de 1,60 sur 25 partants pèse moins qu'un
A/E de 1,08 sur 1500.

Un taux de réussite mesure la **qualité des chevaux confiés** à une écurie ; si
tous ses partants étaient favoris, elle n'a rien ajouté à ce que le marché
savait. Seul l'A/E répond à la question utile.

**Pourquoi cela pourrait avoir tort.**

- **C'est une suppression déguisée en garde-fou, et il faut le dire.** Les
  statistiques A/E françaises avec dénominateur publié sont rares. En pratique,
  cet axe vaudra **zéro presque partout**. J'assume : d'après le §10 de l'annexe,
  zéro est probablement la bonne valeur, et un zéro honnête vaut mieux qu'un
  ±0,35 inventé. Mais c'est une amputation, pas un raffinement.
- **`HUMAN_AE_BETA = 0,55` et `HUMAN_AE_SHRINK_PRIOR = 200` sont des cadrans
  déclarés, non calibrés.**
- **L'A/E vient d'une autre population.** L'attendu est calculé sur les cotes
  d'un échantillon passé, pas sur la course du jour. Un entraîneur avec un bon
  A/E sur des handicaps de province n'a rien prouvé sur un Groupe.

---

## 2. Traçabilité : ce que j'ai touché aux principes de la section 2

Comme la mission l'exige, chaque écart est écrit noir sur blanc. **Aucun
principe n'a été retiré.** Trois ont été touchés :

**2.1 — renforcé.** `human_equipment` passe de *jugé* à *calculé*, sur le chemin
exact ouvert par `ability_class` en v11.8. Conséquence assumée : **un dossier
v11.9 qui portait un `human_equipment` non nul par jugement est désormais
refusé.** C'est une rupture de compatibilité délibérée.

**2.6 — neuf cadrans non calibrés ajoutés**, tous documentés comme tels dans le
code, au même titre que `CLASS_PERFORMANCE_BETA` :
`MARGIN_LENGTH_METRES` (2,4 m — le seul non arbitraire, c'est de la géométrie),
`MARGIN_REFERENCE_SPREAD` (0,025), `MARGIN_WEIGHT` (0,50),
`MARGIN_DEFAULT_DISTANCE_M` (1600), `MARGIN_EPISTEMIC_K` (0,10),
`MARGIN_EPISTEMIC_MAX` (0,25), `HUMAN_AE_SHRINK_PRIOR` (200),
`HUMAN_AE_BETA` (0,55), `HUMAN_AE_MIN_RUNNERS` (30).

**⚠ Un écart réel, à lire attentivement.** `apply_scratch` **écrit désormais
dans un champ déclaré** (`epistemic_sd`). Jusqu'ici le moteur ne réécrivait que
`ability_class`, qui est *calculé*. Ici il modifie une valeur que le collecteur
avait déclarée. Je l'ai fait parce que l'alternative était pire — l'opérateur
était forcé d'inventer une incertitude à T-60 sous contrainte de temps pour
franchir une porte, c'est-à-dire exactement le jugement discrétionnaire que
l'architecture existe pour empêcher. Trois garde-fous : le sens est **toujours
conservateur** (on élargit, jamais on ne resserre, et un test le vérifie sur six
retraits) ; chaque correction est **journalisée** dans
`epistemic_widened_after_scratch` ; et le prompt impose de la mentionner. Si
l'opérateur préfère le refus, la ligne à retirer est isolée et commentée.

---

## 3. Ce que je n'ai pas fait, et pourquoi

- **§5.1 Figures de vitesse et variante de piste.** Non fait. C'est le plus gros
  gisement et le plus gros effort. Il exige un historique de temps par piste ×
  distance × terrain qui **n'existe pas** dans ce projet. Construire la
  mécanique sans la base produirait un module inerte de plus. **C'est le premier
  chantier de la v11.11**, et il commence par de la collecte, pas par du code.
- **§5.2 Sectionnels.** Non fait, même blocage : sans table de par, un
  pourcentage de vitesse terminale n'est pas interprétable. L'annexe le dit.
- **§5.4 Points de vitesse Quirin.** Non fait délibérément : ce serait un canal
  *jugé* de plus, dans une version dont le fil directeur est d'en retirer.
- **§5.5 Tables de corde locales.** Non fait. L'annexe avertit elle-même du
  surajustement, et avec 0 course journalisée nous serions au maximum du risque.
- **§5.7 Modèle hiérarchique bayésien.** Évalué et écarté, pour une raison
  simple : **un modèle hiérarchique met en commun de l'information entre
  niveaux, et nous n'avons aucune information à mettre en commun.** Avec zéro
  course, il se réduirait à ses a priori — c'est-à-dire au jugement, en moins
  lisible. À reconsidérer vers 500 courses.
- **Suppression de modules de gouvernance.** La mission y invitait. Je ne l'ai
  pas fait, et voici mon raisonnement : les neuf modules sont des **portes de
  refus**, pas des producteurs de score. Ils ne coûtent rien par course, ils ne
  peuvent pas gonfler une note, et ce sont eux qui empêchent le mode d'échec le
  plus dangereux du projet — une chaîne d'artefacts parfaitement cohérente et
  vide. Ce qui était réellement supprimable était un **jugement**, pas une
  gouvernance : c'est `human_equipment`, et il est parti.

---

## 4. La conclusion de l'annexe : confirmée

L'annexe affirme que le seul avantage documenté à grande échelle vient de bases
propriétaires et de modèles ajustés dessus, pas de la lecture experte d'une
carte. **Mes recherches la confirment, et je n'apporte rien qui la contredise.**

Benter décrit un modèle logit à neuf facteurs fondamentaux combiné aux
probabilités implicites du public, avec un ajustement hors échantillon de
0,1016 — et son point décisif est que **c'est le modèle combiné qui compte, pas
le modèle de handicapping seul**. C'est l'architecture que ce projet a déjà.
La littérature sur la monnaie tardive va dans le même sens : près de 40 % des
enjeux arrivent dans la dernière minute, et cette monnaie tardive est le
meilleur prédicteur disponible des probabilités réelles.

Il faut en tirer la conséquence honnête, et elle est inconfortable : **rien dans
cette version n'est de nature à battre un prélèvement de 15 %.** La longueur de
battue affine une hiérarchie ; l'A/E retire une hallucination ; la valeur de
clôture ne prédit rien du tout, elle mesure. Le seul progrès revendiqué est
qu'à partir de maintenant, **le système peut se tromper à voix haute et le
savoir en quelques dizaines de courses au lieu de quelques milliers.**

C'est peu. C'est aussi, pour un système à zéro course probante, exactement ce
qui manquait.

---

## 5. Ce que l'opérateur doit faire différemment dès demain

1. Relever **systématiquement les deux snapshots**, T-10 puis T-3. Sans le
   relevé T-10, l'estimateur corrigé n'existe pas et β reste biaisé **vers le
   haut**, c'est-à-dire flatteur.
2. Journaliser le **rapport final** après chaque course. C'est gratuit et c'est
   la seule entrée que la mesure exige.
3. Lire `progress --prices PRICES.jsonl` en section 13, et y lire
   `beta_split_baseline` **en priorité** sur `beta_market_follows_model`.
4. Remplir `beaten_lengths` dès que la source publie l'écart. Table de
   conversion des écarts en toutes lettres dans le prompt, PHASE 2bis.
5. Ne pas chercher d'A/E si la source ne publie pas le dénominateur : laisser
   l'axe à zéro est le comportement correct, pas un échec.

## Annexe — méthode de la simulation de puissance

`power2.py` (répertoire de travail, non versionné) engendre des courses où le
marché T-10 observe la force réelle avec bruit, le marché final en intègre 55 %
de plus (monnaie tardive), et le modèle détient une part `EDGE` du résidu que le
marché T-10 ignore, plus son propre bruit — de sorte qu'il peut être *moins bon*
que le marché tout en contenant une information qu'il n'a pas. Les deux
instruments comparés sont la valeur de clôture et le test réellement pratiqué
par `calibrate` (mélange logit ajusté, holdout temporel 70/30, bootstrap apparié
par jour). Puissance visée 80 % à 95 %.

**Limite honnête de cette simulation :** elle valide la *sensibilité relative*
des deux instruments sous un modèle génératif que j'ai écrit moi-même. Elle ne
dit rien de la taille de l'avantage réel, qui reste inconnue et probablement
nulle.
