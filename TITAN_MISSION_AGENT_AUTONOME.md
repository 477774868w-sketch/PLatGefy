# MISSION TITAN PLAT — agent autonome, carte blanche

Tu reprends un système de prédiction hippique pour le plat français, arrivé en
v11.9 après six itérations. Ta mission est de le faire progresser jusqu'au
niveau des meilleures pratiques mondiales du domaine.

**Tu as carte blanche.** Architecture, modélisation, refonte complète,
suppression de modules entiers, réécriture depuis zéro si tu le juges
préférable : tout est ouvert.

Deux réserves seulement, et je te dis franchement pourquoi.

**La première est absolue.** STAKE reste en mode papier. Le palier 0 impose un
plafond de mise à 0 %, les pools exotiques restent verrouillés jusqu'à 1 000
courses, et la pénalité appliquée tant que `drift_model.fitted` vaut `false`
reste en place. Ce n'est pas une contrainte technique, c'est de l'argent réel :
l'opérateur a construit ce système précisément pour savoir s'il existe un
avantage avant d'en risquer un centime, et ce n'est pas à un agent de lever ce
verrou à sa place.

**La seconde est de la traçabilité, pas une interdiction.** La section 2 liste
des principes qui ont chacun coûté une itération à découvrir. Tu peux les
enfreindre tous. Mais chaque fois que tu en retires ou en contournes un, tu dois
l'écrire noir sur blanc dans ton rapport, avec ton raisonnement — parce qu'un
agent qui supprime une règle supprime aussi le test qui la vérifiait, et son
propre audit affichera alors « tout au vert » sur un système appauvri. C'est le
seul angle mort qu'un auto-audit ne peut pas couvrir.

Le reste de ce document est de l'information, pas des ordres.

---

## 1. Ce que tu reçois

| Fichier | Rôle |
|---|---|
| `titan_plat_v11_9_engine.py` | Moteur sportif, 5 855 lignes, 38 tests |
| `titan_stake_v1_5.py` | Couche de mise, 2 494 lignes, 28 tests |
| `TITAN_PLAT_v11_9_PROMPT_ONESHOT.txt` | Contrat d'exécution de l'opérateur |
| `TITAN_PLAT_FACTEURS_ECOSYSTEME2.pdf` | Annexe : quels facteurs marchent vraiment |
| Ce document | Mission, historique et état d'audit |

Un sixième document existe et n'a pas pu être joint faute de place :
`TITAN_PLAT_v11_1_REFERENTIEL_SCENARIO_BMA3.pdf`, 14 pages, le référentiel
méthodologique du glassbox scénario-BMA. L'essentiel de son contenu est
réimplémenté et commenté dans le moteur. Demande-le à l'opérateur si une
question de méthode reste sans réponse après lecture du code.

**Lis le PDF en premier et intégralement.** L'annexe écosystème contient
un §9 qui liste, par ordre de rentabilité, les chantiers identifiés et non
faits. Elle contient aussi la conclusion la plus importante du projet : la
quasi-totalité des facteurs dont parle l'écosystème du plat ont un rapport
Actual/Expected proche de 1, c'est-à-dire aucun pouvoir prédictif net une fois
le marché pris en compte.

Lance `self-test` sur les deux modules avant de toucher quoi que ce soit.

---

## 2. Les principes actuels, et ce qu'ils ont coûté

Chacun vient d'une erreur réelle. Tu peux tous les remettre en cause. Signale-le
simplement si tu le fais.

**2.1 — Le modèle ne juge pas, il déclare des faits ; le moteur calcule.**
C'est l'architecture centrale, introduite en v11.8. `ability_class` est calculé
depuis `form_lines` (allocation, catégorie, partants, place, date), et toute
divergence entre valeur déclarée et valeur calculée fait échouer le scellement.
Chaque fois que tu ajoutes une variable, demande-toi si elle peut suivre le même
chemin. Une variable que le LLM « estime » est une variable qu'il hallucine.

**2.2 — Ce qui brouille l'information n'augmente jamais le niveau.**
Un incident de parcours, des sorties hétérogènes, une source douteuse :
tout cela élargit `epistemic_sd` et ne remonte aucune note. C'est l'inverse de
l'intuition et c'est la source classique de surestimation. Respecte-le pour
toute variable nouvelle.

**2.3 — Zéro est la bonne valeur quand la preuve ne sépare pas.**
Un canal qui ne sépare pas le peloton doit s'annuler, pas produire du bruit
habillé en signal. Voir `class_ladder.separates` et `structural_clarity`.

**2.4 — Tout invariant doit avoir un test qui exerce sa condition dégénérée.**
Un refus qui n'a jamais été déclenché en test n'existe pas. Ne te contente
jamais d'écrire la règle en commentaire.

**2.5 — `MODEL_VERSION` (actuellement 11.4.0) ne bouge que si la loi de
simulation change.** `ENGINE_VERSION` bouge pour tout le reste. Le découplage
permet de dire « même loi, entrée différente » et il doit rester honnête.

**2.6 — Aucun cadran non calibré ne doit être présenté comme calibré.**
Le moteur contient plusieurs constantes choisies pour reproduire un jugement
intuitif : `CLASS_PERFORMANCE_BETA`, la demi-vie de récence, le plancher de
compétitivité, les seuils de compression du handicap. Elles sont documentées
comme telles. Si tu en ajoutes, documente-les de la même façon.

**2.7 — Le pare-feu de collecte et l'attestation de publication ne
s'assouplissent pas.** Une course ne compte que si son manifeste est scellé et
son engagement publié avant le départ sur un support opposable.

**2.8 — STAKE en mode papier.** Seul point non ouvert, voir l'introduction.

---

## 3. L'état réel, à connaître avant de coder

Six versions, neuf modules de gouvernance, 66 tests. Et :

- courses probantes journalisées : **0**
- entrées au journal de prix : **0**
- `drift_model.fitted` : **false**

Le goulot d'étranglement n'est pas le moteur, c'est l'absence totale de données
accumulées. Garde cela en tête : **une amélioration qui ne pourra pas être
mesurée avant des mois vaut moins qu'un outil qui accélère l'accumulation.**

Tu es donc explicitement autorisé, et encouragé, à travailler sur
l'instrumentation et l'ergonomie de collecte autant que sur la modélisation.

---

## 3bis. État d'audit de la v11.9 — point de départ vérifié

Mesuré sur les fichiers joints, avant toute modification de ta part.

| Contrôle | Résultat |
|---|---|
| `titan_plat_v11_9_engine.py self-test` | PASS 38/38 |
| `titan_stake_v1_5.py self-test` | PASS 28/28 |
| Modules testés / décrits dans le prompt | 9/9 · 9/9 |
| `report_sha256` reproductible sur deux exécutions | oui |
| Invariants matriciels, erreur max | 2,2 × 10⁻¹⁶ |
| Pureté du rendu, clés interdites | 0 |
| Marché exclu du hash sportif | oui |
| Variantes d'ablation valides | 13/13 |
| `except` nus / défauts mutables / globales | 0 / 0 / 0 |
| Chaîne complète en production | `n_countable_races` = 1, `evidential_status` = PROBANTE |
| Mode LITE de bout en bout | fonctionnel, tous nouveaux modules inactifs comme attendu |

Sept audits successifs ont trouvé huit défauts, tous corrigés. Les deux plus
graves sont décrits en section 6 : ce sont les deux dont tu dois te méfier le
plus, parce qu'ils étaient invisibles en self-test.

Constantes déclarées et **non calibrées**, à traiter comme telles :
`CLASS_PERFORMANCE_BETA = 0,45`, demi-vie de récence 240 jours, plancher de
compétitivité 0,25, seuils de compression du handicap 4 et 9 kg,
`DISPERSION_EPISTEMIC_K = 0,30`. Aucune n'a été ajustée sur données ; toutes ont
été choisies pour reproduire un jugement intuitif.

Limites connues et assumées : la longueur de battue n'est pas prise en compte,
le poids porté n'entre pas dans le canal de classe, aucune figure de vitesse
n'existe, et le pare-feu de collecte bloque par domaine et non par page — un
faux positif assumé.

## 4. Ce qui a déjà été fait — ne le refais pas

| Version | Apport |
|---|---|
| v11.6 | Manifeste de collecte (fuite de cotes = échec dur) ; attestation de publication ; `n_countable_races` |
| v11.7 | Canal de transcriptions écrites ; détecteur de confondant documentaire |
| v11.8 | Échelle de classe par allocation, avec clause anti-écrasement |
| v11.9 | Dispersion des lignes de forme ; compression du handicap comme second avis |

Déjà présents par ailleurs : dé-vigorisation de Shin, module d'ablation à 13
variantes, `input_retest`, journal de prix avec `logprice`/`fit-drift`,
calibration hors échantillon avec séparation temporelle, échelle de mise à
paliers, audit post-course.

---

## 5. Pistes de recherche — à instruire, pas à appliquer aveuglément

Ce qui suit est **l'état de nos réflexions, pas une feuille de route**. Ce sont
les pistes qu'un observateur extérieur au domaine a identifiées en lisant la
documentation du projet. Elles sont probablement incomplètes et certaines sont
peut-être fausses.

Documente-toi sérieusement et de façon indépendante sur le handicapping
professionnel du plat. Si ta recherche te conduit ailleurs, va ailleurs : c'est
précisément pour cela qu'on te confie le travail plutôt que de te donner une
liste de tâches. Les pistes ci-dessous ne sont ni classées par priorité ni
exhaustives.

**5.1 — Figures de vitesse et variante de piste.** C'est le socle du
handicapping professionnel anglo-saxon (Beyer, Timeform, Racing Post Ratings).
Convertir un temps brut en figure comparable exige d'estimer la variante du
jour, ce qui exige un historique de temps par piste, distance et terrain. C'est
le §9 n°4 de l'annexe, l'effort le plus élevé et le gisement le plus large.

**5.2 — Sectionnels et pourcentage de vitesse terminale.** France Galop en
publie une partie. La question à trancher est : quelle information reste-t-il
une fois la classe et le rythme pris en compte ?

**5.3 — Longueur de battue.** Le canal de classe actuel traite identiquement un
4e à une encolure et un 4e à quinze longueurs. C'est le défaut le plus
grossier restant, et le moins coûteux à corriger si une source publie les
écarts.

**5.4 — Points de vitesse initiale quantifiés** (approche type Quirin) pour
remplacer la carte de rythme par défaut quand aucun compte-rendu n'est
disponible.

**5.5 — Biais de corde locaux**, conditionnels à la piste, la distance et le
terrain. §9 n°6. Attention au surajustement : ce sont typiquement des effets que
l'on croit voir dans de petits échantillons.

**5.6 — Entraîneurs et jockeys en A/E, jamais en taux de réussite.** §9 n°5.
Un taux de réussite mesure la qualité des chevaux confiés, pas la valeur
ajoutée. C'est une erreur extrêmement répandue.

**5.7 — Modèle hiérarchique bayésien** pour l'aptitude, avec effets aléatoires
par cheval, écurie et entraîneur, plutôt qu'un score additif plafonné. Évalue
honnêtement si le gain justifie la perte de lisibilité : le système est un
glassbox et c'est délibéré.

**5.8 — La valeur de clôture comme étalon.** Dans les paris, la cote finale est
la meilleure estimation publique disponible. Battre la ligne de clôture de
façon répétée est le seul test honnête d'un avantage, bien avant le profit
réalisé. Réfléchis à en faire une métrique de premier plan dans STAKE.

**5.9 — Le prélèvement.** Environ 15 % sur le simple, 36 % sur le trio en
France. Aucune amélioration de modélisation ne rattrape un prélèvement pareil
si l'avantage brut est de quelques points. Tout gain que tu revendiques doit
être comparé à ce seuil, pas à zéro.

**Et une mise en garde.** L'annexe conclut que le seul avantage documenté dans
ce domaine provient de bases de données propriétaires et de modèles ajustés
dessus, pas de la lecture experte d'une carte. Si tes recherches contredisent
cela, apporte des sources ; si elles le confirment, dis-le franchement dans ton
rapport final.

---

## 6. Anti-patterns — les pièges observés sur ce projet

- **Ajouter un module sans pouvoir le mesurer.** En v11.8, la clause
  anti-écrasement empêchait l'ablation de neutraliser `ability_class` : le canal
  le plus récent était le seul dont on ne pouvait pas mesurer l'influence. Un
  module qu'on ne sait pas ablater est un module qu'on croit sur parole.
- **Coder sans mettre à jour le contrat d'exécution.** En v11.9, deux modules
  étaient parfaits dans le moteur et absents du prompt : le collecteur n'aurait
  jamais rempli les champs, et ils ne se seraient jamais déclenchés. Du code
  parfait, inatteignable.
- **Tester les modules isolément.** Le bug le plus grave de la série venait
  d'une interaction : l'échelle de classe étant relative au peloton, un
  non-partant tardif déplaçait la médiane et rendait toute reconstruction
  impossible. Invisible en self-test, fatal en course réelle.
- **Confondre précision affichée et véracité.** Un `INPUT.json` rempli de
  mémoire produit une chaîne d'artefacts parfaitement cohérente et vide.

---

## 7. Livrables attendus

1. Les fichiers modifiés, versionnés proprement, avec les self-tests au vert.
2. Une note de version expliquant chaque ajout, **et notamment pourquoi il
   pourrait avoir tort**.
3. Le prompt one-shot mis à jour pour chaque champ nouveau.
4. Un audit final.

## 8. L'audit final — ce qu'il doit contenir

Ne te contente pas de relire ton code. Attaque-le.

- **Sondes adverses** sur chaque entrée nouvelle : valeur négative, non
  numérique, absente, hors plage, dans le futur, dupliquée.
- **Tests d'interaction** entre modules, et pas seulement de modules isolés.
  Rejoue en particulier le non-partant tardif et l'ablation complète.
- **Contrôle de couverture** : chaque module du code a-t-il son instruction
  correspondante dans le prompt ? Ce contrôle a rattrapé un défaut réel.
- **Chaîne de bout en bout** en paramètres de production :
  `validate → analyze → commit → publish → verify → audit`, en vérifiant que
  `n_countable_races` et `evidential_status` répondent correctement.
- **Contrôles numériques** : reproductibilité du `report_sha256`, invariants
  matriciels, pureté du rendu, exclusion du marché du hash sportif.
- **Mode LITE** vérifié séparément : il sert en usage récréatif et casse
  facilement.
- **Liste honnête de ce que tu n'as pas résolu**, et pourquoi.

Un audit qui ne trouve aucun défaut sur une modification substantielle n'est pas
un bon audit, c'est un audit superficiel. Les sept audits précédents ont trouvé
huit défauts, dont deux critiques.

---

## 9. Le critère de réussite

**C'est à toi de le définir, et de le défendre.**

Explique en ouverture de ton rapport quel critère tu as retenu pour juger que le
système a progressé, et pourquoi ce critère est le bon pour ce projet précis. Un
agent qui ne sait pas dire ce qu'il essaie d'améliorer optimise le nombre de
lignes par défaut.

Une seule remarque, à prendre ou à laisser : sur ce projet, **retirer** peut
valoir mieux qu'ajouter. Six versions ont empilé neuf modules de gouvernance
sans qu'une seule course probante soit produite. Si ton analyse conclut qu'une
partie de cette machinerie ne sert à rien, le dire et la supprimer serait un
résultat, pas un échec.
