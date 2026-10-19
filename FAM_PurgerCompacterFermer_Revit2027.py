# -*- coding: utf-8 -*-
"""FAM_PurgerCompacterFermer_Revit2027 -- noeud PythonNet3 Dynamo.

IN[0] : Directory Path, dossier des familles (*.rfa, recherche recursive).
IN[1] : Boolean facultatif. True autorise la suppression des types inutilises.
IN[2] : Boolean facultatif. True tente de supprimer tous les types de la famille.

Chaque fichier du dossier est purge, enregistre en compact, puis ferme.
Les sauvegardes Revit sont conservees (MaximumBackups = 1). Les fichiers .0001.rfa
et similaires sont exclus de l'inventaire. Le noeud doit etre execute seul, en manuel.
"""
import os
import re
import sys
import types
from contextlib import contextmanager

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitServices")

from Autodesk.Revit import DB
from RevitServices.Persistence import DocumentManager
from System.Collections.Generic import HashSet, List


MAX_PURGE_PASSES = 5
BACKUP_RFA = re.compile(r"\.\d{4,}\.rfa$", re.IGNORECASE)
MATERIAL_PREFIX = "CHU_"


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


def eid_value(element_id):
    return int(element_id.Value)


def parameter_signature(doc):
    """Controle les parametres et formules ; Revit decide quels types sont inutilises."""
    manager = doc.FamilyManager
    return {
        "parameters": sorted(
            (p.Definition.Name, bool(p.IsInstance), str(p.Formula or ""), bool(p.IsShared))
            for p in manager.Parameters
        ),
    }


def assert_signature(doc, reference):
    if parameter_signature(doc) != reference:
        raise RuntimeError("Parametres ou formules modifies par la purge.")


def protected_ids(doc, delete_unused_types):
    """Protege la famille proprietaire, les materiaux CHU_ et, par defaut, les types."""
    protected = {eid_value(doc.OwnerFamily.Id)}
    if not delete_unused_types:
        protected.update(
            eid_value(symbol.Id) for symbol in
            DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)
        )
    for material in DB.FilteredElementCollector(doc).OfClass(DB.Material):
        if not str(material.Name).startswith(MATERIAL_PREFIX):
            continue
        protected.add(eid_value(material.Id))
        for asset_id in (material.AppearanceAssetId, material.StructuralAssetId,
                         material.ThermalAssetId):
            if asset_id != DB.ElementId.InvalidElementId:
                protected.add(eid_value(asset_id))
    return protected


def dotnet_types():
    """Construit une fois le FailureHandler PythonNet3."""
    # Nouvelle cle : un module deja en cache renverrait l'ancien tuple a deux valeurs.
    key = "fam_purge_revit2027_failure_handler_v1"
    if key in sys.modules:
        return sys.modules[key].FailureHandler

    class FailureHandler(DB.IFailuresPreprocessor):
        # Une interface PythonNet3 doit avoir un namespace CLR unique et stable.
        __namespace__ = "FAM.PurgeRevit2027.v1"

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
    module.FailureHandler = FailureHandler
    sys.modules[key] = module
    return FailureHandler


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


def delete_foreign_materials(doc, handler_type, warnings):
    """Supprime les materiaux hors CHU_ un par un : Revit peut en refuser certains."""
    report = {"conserves": 0, "supprimes": [], "refuses": []}
    for material in list(DB.FilteredElementCollector(doc).OfClass(DB.Material)):
        if not material.IsValidObject:
            continue
        name = str(material.Name)
        if name.startswith(MATERIAL_PREFIX):
            report["conserves"] += 1
            continue
        material_id = material.Id
        try:
            in_transaction(doc, "FAM - Suppression materiau", handler_type, warnings,
                           lambda: doc.Delete(material_id))
            report["supprimes"].append(name)
        except Exception as error:
            report["refuses"].append({"materiau": name, "erreur": str(error)})
    return report


def delete_all_owner_types(doc, handler_type, warnings):
    """Mode extreme : supprime chaque type appartenant a la famille ouverte.

    Revit peut refuser la suppression du dernier type. Chaque type est donc traite
    dans sa propre transaction afin que les suppressions deja acceptees soient
    conservees et que les refus soient rapportes sans bloquer le fichier.
    """
    owner_id = eid_value(doc.OwnerFamily.Id)
    types = []
    for symbol in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol):
        try:
            if eid_value(symbol.Family.Id) == owner_id:
                types.append((symbol.Id, str(symbol.Name)))
        except Exception:
            continue

    report = {"demandes": len(types), "supprimes": [], "refuses": []}
    for symbol_id, name in types:
        try:
            def delete_type():
                return list(doc.Delete(symbol_id))
            deleted = in_transaction(doc, "FAM - Suppression forcee type",
                                     handler_type, warnings, delete_type)
            if eid_value(symbol_id) in {eid_value(item) for item in deleted}:
                report["supprimes"].append(name)
            else:
                report["refuses"].append({
                    "type": name,
                    "erreur": "Revit n'a pas confirme la suppression"
                })
        except Exception as error:
            report["refuses"].append({"type": name, "erreur": str(error)})
    return report


def purge_conservatively(doc, handler_type, warnings, delete_unused_types=False):
    """Purge iterative des seuls elements que Revit declare inutilises."""
    if not hasattr(doc, "GetUnusedElements"):
        raise RuntimeError("Document.GetUnusedElements est indisponible dans cette version Revit.")
    signature = parameter_signature(doc)
    protected = protected_ids(doc, delete_unused_types)
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
    root = absolute_path(IN[0] if len(IN) > 0 else None,
                         "IN[0] - dossier de familles")
    delete_unused_types = bool(IN[1]) if len(IN) > 1 and IN[1] is not None else False
    delete_all_types = bool(IN[2]) if len(IN) > 2 and IN[2] is not None else False
    if not os.path.isdir(root):
        raise ValueError("Le dossier de familles est introuvable.")

    paths = get_rfa_files(root)
    app = DocumentManager.Instance.CurrentUIApplication.Application
    if int(app.VersionNumber) < 2027:
        raise RuntimeError("Ce noeud est prevu pour Revit 2027 ou ulterieur.")
    if any(document.IsModifiable for document in app.Documents):
        raise RuntimeError("Une transaction est deja ouverte. Executer ce noeud seul.")

    FailureHandler = dotnet_types()
    closing_errors = []
    report = {
        "noeud": "FAM_PurgerCompacterFermer_Revit2027",
        "revit": str(app.VersionNumber),
        "dossier": root,
        "supprimer_types_inutilises": delete_unused_types,
        "supprimer_tous_types": delete_all_types,
        "fichiers": [],
        "erreurs_fermeture": closing_errors,
        "erreurs_globales": [],
    }

    for path in paths:
        item = {"fichier": path, "statut": "EN_COURS", "avertissements": []}
        report["fichiers"].append(item)
        try:
            item["taille_avant"] = os.path.getsize(path)
            with open_owned_family(app, path, closing_errors) as doc:
                baseline = parameter_signature(doc)
                item["materiaux"] = delete_foreign_materials(
                    doc, FailureHandler, item["avertissements"])
                if delete_all_types:
                    item["types_forces"] = delete_all_owner_types(
                        doc, FailureHandler, item["avertissements"])
                item["purge"] = purge_conservatively(
                    doc, FailureHandler, item["avertissements"],
                    delete_unused_types or delete_all_types)
                if not item["purge"]["stabilise"]:
                    raise RuntimeError("Purge non stabilisee apres {} passes."
                                       .format(MAX_PURGE_PASSES))
                assert_signature(doc, baseline)
                compact_save(doc, path)
            item["taille_apres"] = os.path.getsize(path)
            item["gain_octets"] = item["taille_avant"] - item["taille_apres"]
            item["statut"] = "OK"
        except Exception as error:
            item["statut"] = "ECHEC"
            item["erreur"] = str(error)
        if closing_errors:
            report["erreurs_globales"].append("Arret apres echec de fermeture.")
            break

    report["bilan"] = {
        "reussis": sum(item["statut"] == "OK" for item in report["fichiers"]),
        "echecs": sum(item["statut"] == "ECHEC" for item in report["fichiers"]),
        "non_traites": len(paths) - len(report["fichiers"]),
    }
    return report


try:
    OUT = main()
except Exception as exception:
    OUT = {"statut": "BLOQUE", "erreur": str(exception)}
