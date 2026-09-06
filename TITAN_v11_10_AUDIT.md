# Audit final — TITAN PLAT v11.10.1 / TITAN STAKE v1.6.1

**Rejouable :** `python3 TITAN_v11_10_AUDIT.py` (ou `--json`).
**Résultat :** 103 contrôles, **0 échec**.
Self-tests : moteur **45/45**, mise **36/36**.

| Section | Contrôles |
|---|---|
| 0. Self-tests | 2 |
| 1. Sondes adverses | 33 |
| 2. Interactions entre modules | 11 |
| 3. Couverture du contrat d'exécution | 5 |
| 4. Chaîne de bout en bout | 9 |
| 5. Contrôles numériques | 9 |
| 6. Mode LITE | 8 |
| 7. Couche de mise et valeur de clôture | 12 |

> Un audit qui ne trouve aucun défaut sur une modification substantielle est un
> audit superficiel. Celui-ci en a trouvé **sept**, dont **quatre graves**. Ils
> sont décrits en §1 avant les tableaux verts, parce que c'est la partie utile.
> Trois d'entre eux (D5, D6, D7) sont dans l'instrument que j'ai moi-même écrit
> pour dire la vérité — c'est le sous-système où un défaut coûte le plus cher.

---

## 1. Défauts trouvés et corrigés

### D1 — CRITIQUE. `AXE_human_equipment` n'était pas ablatable

*Trouvé par : test d'interaction « ablation complète ». Invisible en self-test.*

En neutralisant l'axe `human_equipment`, la variante d'ablation laissait
`human_records` en place. La clause anti-écrasement rejetait donc la variante,
et **l'axe le plus récent devenait le seul dont on ne pouvait pas mesurer
l'influence**.

C'est, au mot près, l'anti-pattern n°1 de la mission et la reproduction exacte
du défaut v11.8 sur `ability_class` — que la v11.9 avait corrigé. J'ai réintroduit
le même piège en ajoutant un second canal calculé, sans transposer la leçon.

**Correction :** la variante retire les enregistrements A/E avec l'axe, comme
`AXE_ability_class` retire `form_lines`. Verrouillé par self-test.

**Leçon :** toute règle « la valeur déclarée doit égaler la valeur calculée »
crée mécaniquement un module non ablatable si l'on n'en retire pas aussi les
entrées. La prochaine version qui ajoutera un canal calculé devra y penser
**avant** l'audit.

### D2 — GRAVE, PRÉEXISTANT en v11.9. Le plancher d'incertitude ne survivait pas au non-partant

*Trouvé en attaquant la v11.9 livrée, avant toute modification de ma part.*
*Reproduit sur le moteur d'origine intact.*

La v11.8.2 avait corrigé l'échelle de classe : elle est relative au peloton,
donc un retrait déplace la médiane, donc tous les `ability_class` changent — et
le moteur savait les recalculer, puisqu'ils sont **calculés**.

Le plancher d'incertitude a exactement la même relativité, mais il porte sur
`epistemic_sd`, qui est **déclaré**. Retirer un partant peu incertain fait
monter la médiane, donc le plancher — et un dossier valide **avant** le retrait
devenait invalide **après**, alors que rien concernant le cheval gêné n'avait
changé.

Démonstration sur le moteur v11.9 d'origine, cheval gêné réglé pile à son
plancher :

```
epistemic: [0.72, 0.20, 0.22, 0.24, 0.60, 0.62, 0.64]   médiane 0.60
retrait n2 -> REFUS : exige epistemic_sd >= 0.730, fourni 0.720
retrait n3 -> REFUS  |  retrait n4 -> REFUS  |  retrait n5 -> OK
```

Conséquence opérationnelle : à T-60, l'étape `scratch` du protocole — annoncée
comme « procédure mécanique, jamais de bricolage » — échouait, et l'opérateur
était **poussé à inventer une valeur d'incertitude sous contrainte de temps pour
franchir une porte**. C'est exactement le jugement discrétionnaire que toute
l'architecture existe pour empêcher.

**Correction :** `apply_scratch` porte la contrainte, **uniquement dans le sens
conservateur** (élargit, ne resserre jamais), journalise chaque correction dans
`epistemic_widened_after_scratch`, et le calcul du plancher est extrait dans
`epistemic_floors()` partagé entre validation et reconstruction, pour qu'ils ne
puissent pas diverger. Vérifié sur six retraits différents.

**Cet écart est signalé en §2 de la note de version** : le moteur écrit
désormais dans un champ déclaré, ce qu'il ne faisait pas avant.

### D3 — GRAVE, dans mon propre instrument. Le β de valeur de clôture était biaisé vers le haut

*Trouvé par raisonnement sur l'estimateur, puis reproduit en test.*

`d` et `m` sont tous deux mesurés depuis le marché du snapshot. Tout bruit de
pool dans ce vecteur entre dans les deux avec le même signe et **fabrique de la
covariance même quand le modèle ne sait rien**.

Reproduit : modèle à information **nulle**, marché bruité, 90 courses.

| Estimateur | β | IC 95 % | Verdict |
|---|---|---|---|
| Naïf (base partagée) | **0,321** | [0,291 ; 0,355] | « LE MODÈLE ANTICIPE LE MARCHÉ » — **faux** |
| Bases séparées | **0,0009** | [−0,069 ; 0,062] | INDÉCIS — correct |

Et sur un signal **réel** de 0,55, l'estimateur corrigé retrouve **0,548**.

Pour ce projet, c'est le pire biais possible : il annonce un avantage
inexistant, avec un intervalle de confiance étroit, à un opérateur qui a
construit tout ce système précisément pour ne pas se raconter d'histoires.
**Une lecture naïve du §5.8 de la mission aurait livré cet estimateur.**

**Correction :** estimateur à bases séparées utilisant le relevé T-10 que le
protocole impose déjà, publié à côté, avec la consigne explicite de le croire
en priorité. Verrouillé par self-test.

### D4 — MINEUR. Deux trous de couverture du contrat d'exécution

*Trouvé par le contrôle automatique code ↔ prompt.*

- Le bloc de manifeste s'appelle `collection` dans `INPUT.json`, et le prompt ne
  **nommait jamais la clé** — il décrivait le contenu sans dire où l'écrire.
- La commande `explain-gate` n'était documentée **nulle part**.

Les deux corrigés. Ce contrôle est désormais automatique : il vérifie que chaque
champ collecté, chaque commande CLI, chaque vocabulaire fermé et chaque sortie
publiée est nommé dans le prompt.

### D5 — GRAVE. Shin fausse la mesure d'un déplacement de prix

*Trouvé en poursuivant un faux positif négatif jusqu'à sa cause.*

`clv` dé-vigorait chaque relevé avec Shin. La correction de Shin dépend du
**niveau de cote** et son z est ré-estimé livre par livre : elle ne s'annule
donc pas dans une différence entre deux instants, et le résidu est corrélé à ce
qu'on mesure.

| Prélèvement | Erreur log-ratio, proportionnel | Shin |
|---|---|---|
| 15 % | 6,7 × 10⁻¹⁶ | **0,191** |
| 25 % | 6,7 × 10⁻¹⁶ | **0,338** |
| 36 % | 6,7 × 10⁻¹⁶ | **0,506** |

Sur des données où le modèle ne sait **rien**, l'instrument rendait β = −0,04
**déclaré significatif à 95 %**. Corrigé par normalisation proportionnelle, qui
est exacte pour cet usage : en parimutuel la majoration est un scalaire uniforme
par construction et disparaît en log-ratio centré.

**L'argument reste étroit :** Shin demeure un estimateur de probabilité
défendable et est conservé partout ailleurs. Ce qui est en cause, c'est de
mesurer un *déplacement* avec un outil qui n'est pas un rescalage uniforme.

**Question ouverte, non tranchée :** Shin est également appliqué aux rapports
parimutuels dans `evaluate_race` et dans le diagnostic de marché du moteur. Là
il corrige un biais comportemental réel et son emploi est défendable — mais je
ne l'ai pas vérifié empiriquement, faute de données. À instruire.

### D6 — GRAVE. L'intervalle annonçait 95 % et se trompait 11,7 % du temps

*Trouvé en mesurant le taux de rejet au lieu de le supposer.*

Le bootstrap par percentiles est anti-conservateur avec peu de grappes, et vingt
journées de course, c'est peu.

| Méthode | Taux de rejet réel (nominal 5 %) |
|---|---|
| Percentiles (v1.6.0) | **11,7 %** |
| Student G−1 | 8,3 % |
| **Student G−1 × √(G/(G−1))** | **3,3 %** |

Le point d'estimation était juste (β moyen +0,0004 sous hypothèse nulle, 0,3
erreur-type de zéro) : **c'était l'intervalle qui mentait, pas l'estimateur.**
Puissance intacte après correction (15/15 sur un signal réel de 0,55). Le taux
de rejet est désormais **mesuré à chaque audit**, pas supposé.

### D7 — MOYEN. Un verdict rendu sur un estimateur biaisé

Sans relevé T-10, β simple est biaisé vers le haut. La v1.6.0 publiait quand
même un verdict assorti d'une mise en garde — un faux avantage que personne ne
lit jusqu'au bout. Le module rend maintenant
`NON_MESURABLE_SANS_SNAPSHOT_PRECOCE`. C'est l'idiome du reste du système :
refuser plutôt que commenter.

### Une correction tentée et REJETÉE, consignée pour qu'on ne la refasse pas

J'ai voulu corriger le biais de base partagée par un **test de permutation**,
ce qui aurait dispensé du relevé T-10. Mesuré, puis abandonné : permuter les
vecteurs du modèle injecte l'écart entre deux vérités de course au dénominateur
et sous-estime le nul (0,065 contre 0,313 observé sur les mêmes données nulles) ;
permuter les désaccords ramène le nul à zéro. Les deux cassent l'appariement
intra-course qui **crée** le biais. **Aucun test de permutation ne corrige ce
biais** — seule une base réellement indépendante le peut.

### Et un défaut dans mes propres tests

Une assertion exigeait l'indécision sur trois graines fixes à 95 % — elle échoue
une fois sur sept **par construction**. Un test statistique écrit comme un test
déterministe est un test qui finira par être désactivé pour la mauvaise raison.
Remplacé par la propriété stable (absence de biais) ; la mesure du taux de rejet
est passée dans l'audit.

---

## 2. Sondes adverses (33)

Chaque entrée nouvelle attaquée avec des valeurs hostiles. Toutes refusées avec
un message nommant le champ — **un refus, jamais un crash**.

**`beaten_lengths` / `distance_m` (11)** — négatif, non numérique, NaN, infini,
booléen, hors plage, vainqueur déclaré battu, distance sans marge, distance hors
plage, distance non numérique, distance négative.

**`human_records` (22)** — dénominateur sous le minimum / négatif / non entier /
booléen, victoires supérieures au dénominateur / négatives, attendu nul /
négatif / non numérique / supérieur au dénominateur / NaN, fenêtre hors liste /
absente / non entière, périmètre inconnu / absent, contexte absent, preuve
inconnue du registre, **A/E adossé à un tier H** (une rumeur ne déplace pas un
niveau), observation postérieure à `as_of`, périmètre dupliqué (double
comptage), liste vide.

---

## 3. Interactions entre modules (11)

Le pire bug de la série venait d'une interaction, pas d'un module.

- **Non-partant tardif** rejoué sur **6 retraits différents**, sur un dossier
  portant marges *et* A/E : 6/6 reconstruits sans intervention manuelle.
- **Un retrait n'a jamais resserré une incertitude** (vérifié sur tous).
- **Ablation complète** : 14 variantes, toutes valides (c'est D1 qui a échoué ici).
- `MARGE_LONGUEURS` présente et d'effet mesurable.
- **La marge sépare ce que le rang ne séparait pas** — le cœur du §5.3 :

  | Chevaux de même place, même allocation, même peloton | `ability_class` |
  |---|---|
  | v11.9 (rang seul) | 0,0 pour **tous** |
  | v11.10, battu de 0,2 L | **+0,039** |
  | v11.10, battu de 20 L | **−0,072** |

- **Anti-écrasement croisé** : modifier le fait sous-jacent sans recopier la
  valeur calculée est refusé, pour l'A/E comme pour la marge.

---

## 4. Chaîne de bout en bout, paramètres de production (9)

`validate → analyze → commit → publish → verify-journal → audit`, sur un dossier
FULL à 2000 mondes × 100 courses, départ à T+40, avec marges et A/E.

- `invariant_check.ok` **true**, `render_purity_check.ok` **true**
- `verify-journal` → **`n_countable_races` = 1**, `chain_ok` true
- **Cas négatif** : sans attestation de publication → **`n_countable_races` = 0**
- `audit` → **`evidential_status` = PROBANTE**

Le pare-feu s'est déclenché contre moi pendant l'audit : relancer `commit` sur
un journal déjà peuplé a été refusé pour republication silencieuse. C'était
correct — l'audit repart désormais d'un répertoire vierge.

---

## 5. Contrôles numériques (9)

- `report_sha256` **reproductible** sur deux exécutions ; `sport_core_sha256` idem
- Invariants matriciels : erreur max **1,1 × 10⁻¹⁶**
- Pureté du rendu : **0** clé interdite
- **Marché exclu du hash sportif**
- **Graine découplée d'`ENGINE_VERSION`** (forcée à 99.99.0 : graine inchangée)
- **`MODEL_VERSION` = 11.4.0, inchangé** — aucune loi de simulation touchée
- **Rétro-compatibilité numérique** : sans marge déclarée, la note de sortie est
  identique à la v11.9 au chiffre près

---

## 6. Mode LITE (8)

Vérifié séparément, de bout en bout. `expand` → `validate` → `analyze` complet,
`data_grade` forcé à C, `fill_mode` = LITE, pureté du rendu OK, et surtout :
**les trois modules v11.10 sont inactifs, pas cassés** — échelle de classe
inactive, aucun `human_records`, aucune marge.

---

## 7. Couche de mise et réserve (12)

- CLV détecte un modèle qui anticipe (β = 0,637, IC [0,601 ; 0,670])
- CLV reste **indécis sur du bruit pur** (β = 0,005)
- Biais de base partagée reproduit, puis corrigé (D3)
- Journal de prix altéré → **détecté** ; journal de tickets tronqué → **détecté**
- Course sans rapport final → **écartée et comptée**, jamais devinée

**La réserve absolue, vérifiée quatre fois :**

| Contrôle | Résultat |
|---|---|
| La valeur de clôture n'ouvre aucune porte | échelle **identique** avec et sans elle |
| Palier 0 | plafond **0 %** |
| Pools exotiques sous 1000 courses | **verrouillés** (0, 50, 200, 500, 999) |
| Pénalité tant que `drift_model.fitted` est faux | **en place** |

---

## 8. Liste honnête de ce que je n'ai pas résolu

1. **Aucune figure de vitesse, aucune variante de piste (§5.1).** Le plus gros
   gisement du domaine, non entamé. Il exige un historique de temps par piste ×
   distance × terrain qui n'existe pas ici. Écrire la mécanique sans la base
   aurait produit un module inerte de plus.
2. **Aucun sectionnel exploitable (§5.2).** Même blocage : sans table de par, un
   pourcentage de vitesse terminale n'est pas interprétable.
3. **La marge ignore le rythme.** Cinq longueurs dans une course lente ne valent
   pas cinq longueurs dans une course rapide. Corrigeable seulement avec la
   table de par du point 1.
4. ~~Le plancher de marge est contournable par la paresse uniforme.~~
   **Corrigé en v11.10.1** par une inflation calculée par le moteur, que le
   collecteur ne peut pas satisfaire en élargissant `epistemic_sd`.
5. **L'axe `human_equipment` vaudra zéro presque partout**, faute de statistiques
   A/E françaises publiées avec dénominateur. C'est une amputation assumée, pas
   un raffinement.
6. **Dix cadrans non calibrés de plus.** Aucun n'a été ajusté sur données. Ils
   sont documentés comme tels, ce qui les rend honnêtes, pas justes.
7. **β n'est pas validé sur données réelles.** Zéro course journalisée : tout ce
   qui précède est synthétique. La simulation de puissance valide la
   *sensibilité relative* des instruments sous un modèle génératif que j'ai
   écrit moi-même — elle ne dit rien de l'avantage réel, qui reste inconnu et
   probablement nul.
8. **Aucun gain de rentabilité n'est revendiqué, ni démontrable.** Le seuil de
   comparaison est le prélèvement (≈ 15 % en simple, ≈ 36 % en trio), pas zéro.
   Rien dans cette version n'est de nature à le franchir.
9. **Le pare-feu de collecte bloque par domaine pour les jetons d'opérateur.**
   Vérifié : il tokenise l'URL entière, donc les jetons de SUJET (cotes,
   rapports) sont déjà sensibles au chemin ; seuls les jetons d'OPÉRATEUR (pmu,
   geny…) condamnent un domaine entier. Non corrigé **délibérément** : assouplir
   un pare-feu de collecte contredirait le principe 2.7, et le faux positif est
   du bon côté.
11. **L'emploi de Shin ailleurs dans le système n'est pas vérifié.** Il est
   défendable en principe (biais comportemental réel) mais je n'ai pas pu le
   mesurer faute de données. Voir D5.
10. **Je n'ai supprimé aucun module de gouvernance**, alors que la mission y
    invitait. Raisonnement en §3 de la note de version : ce sont des portes de
    refus, pas des producteurs de score. Ce qui était supprimable était un
    jugement — `human_equipment` — et il est parti.

---

## 9. Verdict

Le système fait maintenant quelque chose qu'il ne savait pas faire : **se
mesurer en dizaines de courses au lieu de milliers, sans engager un euro.** Deux
défauts graves ont été fermés, dont un préexistant qui rendait la procédure de
non-partant impraticable, et un que j'avais moi-même introduit dans l'instrument
censé dire la vérité.

Ce que le système ne fait toujours pas : gagner de l'argent. Rien ici ne le
prétend, et la conclusion de l'annexe reste, après vérification indépendante,
la description exacte de la situation.
