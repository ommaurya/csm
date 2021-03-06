
from constants import PackageType
from platform_matcher import get_platform, get_release, UNKNOWN
from smu_info_loader import SMUInfoLoader
from smu_advisor import get_excluded_supersede_list 
from smu_advisor import get_missing_required_prerequisites
from smu_advisor import get_dict_from_list

SMU_INDICATOR = 'CSC'
SP_INDICATOR = '.sp'
    
"""
Returns the package type.  Available package types are defined in PackageType.
"""
def get_package_type(name):
    """
    Only ASR9K supports Service Packs concept
    Example: asr9k-px-4.3.2.sp-1.0.0 or asr9k-px-4.3.2.k9-sp-1.0.0
    """
    if name.find(SMU_INDICATOR) != -1:
        return PackageType.SMU
    elif name.find(SP_INDICATOR) != -1:
        return PackageType.SERVICE_PACK
    else:
        return PackageType.PACKAGE
    
"""
Given a package name, try to derive a name which can be used to lookup a SMU or SP
in the SMU meta file.
 
However, there is no guarantee that the correct name can be derived. That depends
on the given name if it is within the parsing criteria.
"""
def get_smu_lookup_name(name):
    name = name.strip()
    package_type = get_package_type(name)
    if package_type != PackageType.SMU and package_type != PackageType.SERVICE_PACK:
        return name
    
    # The worst case scenario of the name could be "disk0:asr9k-px-4.2.1.CSCud90009-1.0.0.pie"
    name = name.replace('.pie','')
    
    # Skip the location string if found
    pos = name.find(':')
    if pos != -1:
        name = name[pos+1:]
        
    # For SMU, the resultant name needs to be in this format: "asr9k-px-4.2.1.CSCud90009".
    # However, on the device, the SMU is in this format: "asr9k-px-4.2.1.CSCud90009-1.0.0".
    pos = name.find(SMU_INDICATOR)
    if pos != -1:
        # Strip the -1.0.0 string if found
        try:
            # index may throw ValueError if substring not found
            pos2 = name.index('-', pos)
            if pos2 != -1:
                name = name[:pos2]
        except:
            pass
            
    return name

"""
The smu_info_dict has the following format
   smu name ->  set()
"""
def get_unique_set_from_dict(smu_info_dict):
    resultant_set = set()
    for smu_set in smu_info_dict.values():
        for smu_name in smu_set:
            if smu_name not in resultant_set:
                resultant_set.add(smu_name)
            
    return resultant_set
                
def get_missing_prerequisite_list(smu_list):
    result_list = []   
    platform, release = get_platform_and_release(smu_list)
    
    if platform == UNKNOWN or release == UNKNOWN:
        return result_list
    
    # Load the SMU information
    smu_loader = SMUInfoLoader(platform, release)
    smu_info_list= []
    smu_name_set = set()
    
    for line in smu_list:
        smu_name = get_smu_lookup_name(line)
        smu_info = smu_loader.get_smu_info(smu_name)
        
        if smu_info is None or smu_name in smu_name_set:  
            continue

        smu_name_set.add(smu_name)
        smu_info_list.append(smu_info)
        
    if len(smu_info_list) > 0:
        # Exclude all the superseded SMUs in smu_info_list
        excluded_supersede_list = get_excluded_supersede_list(smu_info_list)
       
        missing_required_prerequisite_dict = \
            get_missing_required_prerequisites(smu_loader, excluded_supersede_list)
        
        missing_required_prerequisite_set = get_unique_set_from_dict(missing_required_prerequisite_dict)
        for pre_requisite_smu in missing_required_prerequisite_set:
            result_list.append(pre_requisite_smu + '.' + smu_loader.file_suffix)
                
    return result_list

"""
Given a package list (SMU/SP/Pacages), return the platform and release.
"""
def get_platform_and_release(package_list):
    platform = UNKNOWN
    release = UNKNOWN
    
    # Identify the platform and release
    for line in package_list:
        platform = get_platform(line)
        release = get_release(line)
        
        if platform != UNKNOWN and release != UNKNOWN:
            break
        
    return platform, release

"""
Given a SMU list, return a dictionary which contains
key: smu name in smu_list
value: cco filename  (can be None if smu_name is not in the XML file)
""" 
def get_download_info_dict(smu_list):
    download_info_dict = {}
    platform, release = get_platform_and_release(smu_list)
    
    if platform == UNKNOWN or release == UNKNOWN:
        return download_info_dict, None
    
    # Load the SMU information
    smu_loader = SMUInfoLoader(platform, release)
    for smu_name in smu_list:
        lookup_name = get_smu_lookup_name(smu_name)
        smu_info = smu_loader.get_smu_info(lookup_name)
        if smu_info is not None:
            # Return back the same name (i.e. smu_name)
            download_info_dict[smu_name] = smu_info.cco_filename
        else:
            download_info_dict[smu_name] = None
            
    return download_info_dict, smu_loader
    
"""
Returns the optimize list given the SMU/SP list.
A smu_list may contain packages, SMUs, SPs, or junk texts.
This method is used to support the SMU Optimize feature
"""
def get_optimize_list(smu_list):
    error_list = []
    result_list = []
    
    # Identify the platform and release
    platform, release = get_platform_and_release(smu_list)
    
    if platform == UNKNOWN or release == UNKNOWN:
        return result_list, error_list
    
    # Load the SMU information
    smu_loader = SMUInfoLoader(platform, release)
    file_suffix = smu_loader.file_suffix
    smu_info_list= []
    smu_name_set = set()
    
    for line in smu_list:
        smu_name = get_smu_lookup_name(line)
        smu_info = smu_loader.get_smu_info(smu_name)
        
        if smu_info is None:
            error_list.append(smu_name)
            continue
        
        if smu_name in smu_name_set:
            continue
    
        smu_name_set.add(smu_name)
        smu_info_list.append(smu_info)
        
    if len(smu_info_list) > 0:
        # Exclude all the superseded SMUs in smu_info_list
        excluded_supersede_list = get_excluded_supersede_list(smu_info_list)
       
        missing_required_prerequisite_dict = \
            get_missing_required_prerequisites(smu_loader, excluded_supersede_list)
        
        missing_required_prerequisite_set = get_unique_set_from_dict(missing_required_prerequisite_dict)
        for pre_requisite_smu in missing_required_prerequisite_set:
            result_list.append(pre_requisite_smu + '.' + file_suffix + ' (A Missing Pre-requisite)')
            
        excluded_supersede_dict = get_dict_from_list(excluded_supersede_list)
        
        for smu_info in smu_info_list:
            if smu_info.name not in excluded_supersede_dict:
                result_list.append(smu_info.name + '.' + file_suffix + ' (Superseded)')
            else:
                result_list.append(smu_info.name + '.' + file_suffix)
                
    return result_list, error_list

if __name__ == '__main__':
    pass