# -*- coding: utf-8 -*-
"""FAM_OptimiserBibliotheque_Revit2027 -- noeud PythonNet3 Dynamo.

IN[0] : Directory Path, dossier des familles hotes (*.rfa, recherche recursive).
IN[1] : Directory Path, dossier des familles imbriquees de reference (*.rfa, recursive).

Les fichiers sources sont modifies : les imbriquees sont compactees avant les hotes.
Les sauvegardes Revit sont conservees (MaximumBackups = 1). Les fichiers .0001.rfa
et similaires sont exclus de l'inventaire. Le noeud doit etre execute seul, en manuel.
"""
import os
import re
import sys
import json
import types
from collections import defaultdict
from contextlib import contextmanager

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitServices")

from Autodesk.Revit import DB
from RevitServices.Persistence import DocumentManager
from System.Collections.Generic import HashSet, List


MAX_PURGE_PASSES = 10
BACKUP_RFA = re.compile(r"\.\d{4,}\.rfa$", re.IGNORECASE)


def absolute_path(value, input_name):
    """Accepte le texte de Directory Path ou un objet Directory Dynamo/.NET."""
    if value is None:
        raise ValueError("{} n'est pas connecte.".format(input_name))
    raw_value = value
    # La sortie Directory Path est normalement une chaine. Cette extraction rend aussi
    # le noeud compatible avec Directory.FromPath et System.IO.DirectoryInfo.
    for attribute in ("Path", "FullName"):
        if hasattr(value, attribute):
            candidate = getattr(value, attribute)
            if candidate:
                value = candidate
                break
    path = str(value).strip().strip('"')
    if not path or not os.path.isabs(path):
        raise ValueError(
            "{} doit recevoir le texte d'un chemin absolu. Valeur recue : {!r}"
            .format(input_name, str(raw_value))
        )
    # Ne pas appeler normcase ici : sous Windows il met le chemin en minuscules.
    # Ce chemin est aussi transmis a SaveAs et doit conserver sa casse d'origine.
    return os.path.realpath(os.path.abspath(path))


def path_key(path):
    """Cle de comparaison Windows insensible a la casse, jamais utilisee pour SaveAs."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_in(path, root):
    try:
        path_key_value = path_key(path)
        root_key_value = path_key(root)
        return os.path.commonpath([path_key_value, root_key_value]) == root_key_value
    except ValueError:
        return False


def get_rfa_files(root):
    files = set()
    for current, folders, names in os.walk(root):
        # Ne suit aucun lien de dossier : il pourrait sortir de la bibliotheque cible.
        folders[:] = sorted(f for f in folders
                            if not os.path.islink(os.path.join(current, f)))
        for name in sorted(names):
            if not name.lower().endswith(".rfa") or BACKUP_RFA.search(name):
                continue
            # os.walk renvoie le nom reel du fichier ; le conserver pour la sauvegarde.
            path = os.path.realpath(os.path.join(current, name))
            if not is_in(path, root):
                raise RuntimeError("Lien de fichier hors dossier : " + path)
            files.add(path)
    return sorted(files)


def element_name(doc, element, fallback=None):
    """Lit le nom sans accepter silencieusement une chaine vide.

    Pour les OwnerFamily de documents ouverts en arriere-plan, Name peut etre vide
    dans Revit 2027. Le parametre systeme contient alors le nom de famille.
    """
    # Le Family ne renseigne pas toujours son propre nom dans un document de famille.
    # Les FamilySymbol associes, eux, portent SYSTEM_FAMILY_NAME de facon fiable.
    try:
        for symbol_id in element.GetFamilySymbolIds():
            symbol = doc.GetElement(symbol_id)
            if symbol is None:
                continue
            parameter = symbol.get_Parameter(DB.BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)
            if parameter is not None:
                name = parameter.AsString() or parameter.AsValueString()
                if name and str(name).strip():
                    return str(name).strip()
            name = str(symbol.FamilyName or "").strip()
            if name:
                return name
    except Exception:
        pass
    parameter = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME)
    if parameter is not None:
        name = parameter.AsString() or parameter.AsValueString()
        if name and str(name).strip():
            return str(name).strip()
    name = str(element.Name or "").strip()
    if name:
        return name
    if fallback and str(fallback).strip():
        return str(fallback).strip()
    raise RuntimeError("Nom de famille vide ou inaccessible (ElementId {})."
                       .format(eid_value(element.Id)))


def eid_value(element_id):
    return int(element_id.Value)


def identity(doc, family, fallback_name=None):
    """Identite securisee : nom, categorie, et caractere partage."""
    category = family.FamilyCategory
    shared_parameter = family.get_Parameter(DB.BuiltInParameter.FAMILY_SHARED)
    is_shared = (shared_parameter is not None and
                 shared_parameter.AsInteger() == 1)
    return (
        element_name(doc, family, fallback_name).casefold(),
        eid_value(category.Id) if category else None,
        is_shared,
    )


def nested_families(doc):
    owner_id = eid_value(doc.OwnerFamily.Id)
    return [family for family in DB.FilteredElementCollector(doc).OfClass(DB.Family)
            if eid_value(family.Id) != owner_id and not family.IsInPlace]


def parameter_signature(doc):
    """Controle que la purge ne touche ni types ni parametres/formules de l'hote."""
    manager = doc.FamilyManager
    return {
        "types": sorted(t.Name for t in manager.Types),
        "parameters": sorted(
            (p.Definition.Name, bool(p.IsInstance), str(p.Formula or ""), bool(p.IsShared))
            for p in manager.Parameters
        ),
    }


def assert_signature(doc, reference):
    if parameter_signature(doc) != reference:
        raise RuntimeError("Types, parametres ou formules modifies par la purge.")


def protected_ids(doc):
    """Ne purge pas les familles imbriquees ni leurs types : utiles a la bibliotheque."""
    protected = {eid_value(doc.OwnerFamily.Id)}
    for class_type in (DB.Family, DB.FamilySymbol):
        protected.update(eid_value(element.Id) for element in
                         DB.FilteredElementCollector(doc).OfClass(class_type))
    return protected


def dotnet_types():
    """Construit une fois les implementations d'interfaces PythonNet3."""
    key = "fam_optimiser_revit2027_interfaces_v1"
    if key in sys.modules:
        module = sys.modules[key]
        return module.LoadOptions, module.FailureHandler

    class LoadOptions(DB.IFamilyLoadOptions):
        # Une interface PythonNet3 doit avoir un namespace CLR unique et stable.
        __namespace__ = "FAM.OptimiserRevit2027.v1"

        def __init__(self):
            super().__init__()

        def OnFamilyFound(self, familyInUse, overwriteParameterValues):
            # Conserve les valeurs des types de l'hote.
            return (True, False)

        def OnSharedFamilyFound(self, sharedFamily, familyInUse, source,
                                overwriteParameterValues):
            # Prend la definition de la bibliotheque, sans ecraser les valeurs hote.
            return (True, DB.FamilySource.Family, False)

    class FailureHandler(DB.IFailuresPreprocessor):
        __namespace__ = "FAM.OptimiserRevit2027.v1"

        def __init__(self):
            super().__init__()
            self.messages = []

        def PreprocessFailures(self, accessor):
            has_error = False
            for message in accessor.GetFailureMessages():
                self.messages.append(message.GetDescriptionText())
                if message.GetSeverity() == DB.FailureSeverity.Warning:
                    accessor.DeleteWarning(message)
                else:
                    has_error = True
            return (DB.FailureProcessingResult.ProceedWithRollBack if has_error
                    else DB.FailureProcessingResult.Continue)

    module = types.ModuleType(key)
    module.LoadOptions = LoadOptions
    module.FailureHandler = FailureHandler
    sys.modules[key] = module
    return LoadOptions, FailureHandler


@contextmanager
def open_owned_family(app, path, closing_errors):
    """Ouvre et ferme uniquement des documents que ce noeud a ouverts."""
    document = None
    try:
        for opened in app.Documents:
            # Le projet actif peut etre detache et exposer seulement son nom de fichier.
            # Il ne fait pas partie du lot : seuls les documents de familles importent ici.
            if not opened.IsFamilyDocument:
                continue
            opened_path = str(opened.PathName or "").strip()
            if opened_path and os.path.isabs(opened_path) and \
                    path_key(opened_path) == path_key(path):
                raise RuntimeError("La famille est deja ouverte dans Revit : " + path)
        document = app.OpenDocumentFile(path)
        if not document.IsFamilyDocument:
            raise RuntimeError("Le fichier n'est pas une famille Revit : " + path)
        if document.IsReadOnly or document.IsModifiable:
            raise RuntimeError("Famille en lecture seule ou modifiable : " + path)
        yield document
    finally:
        if document is not None and document.IsValidObject:
            try:
                document.Close(False)
            except Exception as error:
                closing_errors.append({"fichier": path, "erreur": str(error)})


def in_transaction(doc, name, handler_type, warnings, operation):
    transaction = DB.Transaction(doc, name)
    handler = handler_type()
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise RuntimeError("Transaction non demarree : " + name)
        options = transaction.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(handler)
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
        value = operation()
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise RuntimeError("Transaction annulee : " + name)
        return value
    finally:
        warnings.extend(str(item) for item in handler.messages)
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        transaction.Dispose()


def purge_conservatively(doc, handler_type, warnings):
    """Purge iterative via l'API native, en preservant familles, symboles et structure."""
    if not hasattr(doc, "GetUnusedElements"):
        raise RuntimeError("Document.GetUnusedElements est indisponible dans cette version Revit.")
    signature = parameter_signature(doc)
    protected = protected_ids(doc)
    deleted_total = 0

    for pass_number in range(1, MAX_PURGE_PASSES + 1):
        unused = list(doc.GetUnusedElements(HashSet[DB.ElementId]()))
        candidates = [element_id for element_id in unused
                      if eid_value(element_id) not in protected]
        if not candidates:
            return {"passes": pass_number - 1, "supprimes": deleted_total,
                    "stabilise": True, "proteges": len(unused)}

        def delete_candidates():
            ids = List[DB.ElementId]()
            for element_id in candidates:
                ids.Add(element_id)
            deleted = list(doc.Delete(ids))
            deleted_values = {eid_value(element_id) for element_id in deleted}
            if protected.intersection(deleted_values):
                raise RuntimeError("La purge a touche un element protege.")
            doc.Regenerate()
            assert_signature(doc, signature)
            return len(deleted)

        deleted_count = in_transaction(
            doc, "FAM - Purge unused", handler_type, warnings, delete_candidates)
        deleted_total += deleted_count
        if deleted_count == 0:
            raise RuntimeError("La purge ne fait plus de progres.")

    remaining = [element_id for element_id in
                 doc.GetUnusedElements(HashSet[DB.ElementId]())
                 if eid_value(element_id) not in protected]
    return {"passes": MAX_PURGE_PASSES, "supprimes": deleted_total,
            "stabilise": not bool(remaining), "restants": len(remaining)}


def compact_save(doc, path):
    """Enregistre le meme fichier source avec Compact et une sauvegarde Revit."""
    if doc.IsModifiable:
        raise RuntimeError("Transaction ouverte avant sauvegarde : " + path)
    options = DB.SaveAsOptions()
    try:
        options.Compact = True
        options.OverwriteExistingFile = True
        options.MaximumBackups = 1
        doc.SaveAs(path, options)
    finally:
        options.Dispose()


def main():
    if len(IN) < 2:
        raise ValueError("Deux Directory Path sont requis : hotes en IN[0], imbriquees en IN[1].")
    host_root = absolute_path(IN[0] if len(IN) > 0 else None, "IN[0] - familles hotes")
    nested_root = absolute_path(IN[1] if len(IN) > 1 else None, "IN[1] - familles imbriquees")
    if not os.path.isdir(host_root) or not os.path.isdir(nested_root):
        raise ValueError("Un des dossiers sources est introuvable.")
    if host_root == nested_root:
        raise ValueError("Les dossiers hotes et imbriques doivent etre distincts.")

    nested_paths = get_rfa_files(nested_root)
    all_host_paths = get_rfa_files(host_root)
    nested_set = set(nested_paths)
    # Un sous-dossier d'imbriquees peut etre dans les hotes : ne pas le traiter comme hote.
    host_paths = [path for path in all_host_paths if path not in nested_set]
    app = DocumentManager.Instance.CurrentUIApplication.Application
    report = {
        "noeud": "FAM_OptimiserBibliotheque_Revit2027",
        "revit": str(app.VersionNumber),
        "dossier_hotes": host_root,
        "dossier_imbriquees": nested_root,
        "imbriquees": nested_paths,
        "hotes": host_paths,
        "fichiers": [],
        "erreurs_fermeture": [],
        "erreurs_globales": [],
    }
    if int(app.VersionNumber) < 2027:
        raise RuntimeError("Ce noeud est prevu pour Revit 2027 ou ulterieur.")
    if any(document.IsModifiable for document in app.Documents):
        raise RuntimeError("Une transaction est deja ouverte. Executer ce noeud seul.")

    LoadOptions, FailureHandler = dotnet_types()
    load_options = LoadOptions()
    closing_errors = report["erreurs_fermeture"]
    source_by_identity = {}
    metadata = {}
    outcomes = {}

    try:
        # Etape 1 : lecture des identites et des dependances des imbriquees.
        duplicates = defaultdict(list)
        for path in nested_paths:
            with open_owned_family(app, path, closing_errors) as doc:
                # Title est le nom de fichier sans extension : repli fiable pour OwnerFamily.
                owner = identity(doc, doc.OwnerFamily, doc.Title)
                metadata[path] = {
                    "owner": owner,
                }
                duplicates[owner].append(path)
        ambiguous = {str(key): value for key, value in duplicates.items() if len(value) > 1}
        if ambiguous:
            raise RuntimeError("Deux sources imbriquees ont la meme identite : " +
                               json.dumps(ambiguous, ensure_ascii=False))
        source_by_identity = {key: paths[0] for key, paths in duplicates.items()}

        # Les fichiers du dossier imbriquees sont des sources de reference. Ils sont
        # compactes individuellement ; on ne tente pas de recharger les familles
        # systeme qu'ils contiennent (marques, etiquettes, annotations, etc.).
        processing_order = nested_paths

        def process(path, role, expected_owner=None, reload_nested=False):
            item = {"fichier": path, "role": role, "statut": "EN_COURS",
                    "recharges": [], "sans_source": [], "avertissements": []}
            report["fichiers"].append(item)
            try:
                item["taille_avant"] = os.path.getsize(path)
                with open_owned_family(app, path, closing_errors) as doc:
                    if expected_owner is not None and identity(doc, doc.OwnerFamily, doc.Title) != expected_owner:
                        raise RuntimeError("Identite de famille inattendue : " + path)
                    baseline = parameter_signature(doc)
                    for family in nested_families(doc) if reload_nested else []:
                        try:
                            child_id = identity(doc, family)
                        except RuntimeError:
                            # Famille systeme sans nom exploitable : elle n'est pas une
                            # candidate au remplacement depuis le dossier de reference.
                            item.setdefault("familles_non_identifiees", []).append(
                                eid_value(family.Id))
                            continue
                        child_path = source_by_identity.get(child_id)
                        if child_path is None:
                            item["sans_source"].append(element_name(doc, family))
                            continue
                        if outcomes.get(child_path, {}).get("statut") != "OK":
                            raise RuntimeError("Source imbriquee non prete : " + child_path)
                        # LoadFamily est appele hors transaction : condition requise par Revit.
                        load_result = doc.LoadFamily(child_path, load_options)
                        # PythonNet3 expose l'overload LoadFamily(..., out Family)
                        # sous la forme (bool success, Family loaded).
                        if isinstance(load_result, tuple):
                            success = bool(load_result[0]) if len(load_result) > 0 else False
                            loaded = load_result[1] if len(load_result) > 1 else None
                        else:
                            # Compatibilite avec une implementation retournant directement Family.
                            success = load_result is not None
                            loaded = load_result
                        if not success or loaded is None:
                            raise RuntimeError("Echec de rechargement : " + child_path)
                        if identity(doc, loaded) != child_id:
                            raise RuntimeError("Rechargement avec identite incoherente : " + child_path)
                        item["recharges"].append(child_path)

                    def validate():
                        doc.Regenerate()
                        assert_signature(doc, baseline)
                    in_transaction(doc, "FAM - Verification", FailureHandler,
                                   item["avertissements"], validate)
                    item["purge"] = purge_conservatively(doc, FailureHandler,
                                                           item["avertissements"])
                    if not item["purge"]["stabilise"]:
                        raise RuntimeError("Purge non stabilisee apres {} passes.".format(MAX_PURGE_PASSES))
                    assert_signature(doc, baseline)
                    compact_save(doc, path)
                item["taille_apres"] = os.path.getsize(path)
                item["gain_octets"] = item["taille_avant"] - item["taille_apres"]
                item["statut"] = "OK"
            except Exception as error:
                item["statut"] = "ECHEC"
                item["erreur"] = str(error)
            outcomes[path] = item

        # Etape 2 : optimisation des sources imbriquees.
        for path in processing_order:
            process(path, "IMBRIQUEE", metadata[path]["owner"], reload_nested=False)
            if closing_errors:
                raise RuntimeError("Arret apres echec de fermeture de document.")

        # Etape 3 : rechargement des imbriquees optimisees dans les hotes, purge et compactage.
        for path in host_paths:
            process(path, "HOTE", reload_nested=True)
            if closing_errors:
                raise RuntimeError("Arret apres echec de fermeture de document.")
    except Exception as error:
        report["erreurs_globales"].append(str(error))
    finally:
        report["bilan"] = {
            "reussis": sum(item["statut"] == "OK" for item in report["fichiers"]),
            "echecs": sum(item["statut"] == "ECHEC" for item in report["fichiers"]),
            "non_traites": len(nested_paths) + len(host_paths) - len(report["fichiers"]),
        }
    return report


try:
    OUT = main()
except Exception as exception:
    OUT = {"statut": "BLOQUE", "erreur": str(exception)}
