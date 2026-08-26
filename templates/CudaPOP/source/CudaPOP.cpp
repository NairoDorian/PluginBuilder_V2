/* Shared Use License: This file is owned by Derivative Inc. (Derivative)
* and can only be used, and/or modified for use, in conjunction with
* Derivative's TouchDesigner software, and only if you are a licensee who has
* accepted Derivative's TouchDesigner license or assignment agreement
* (which also govern the use of this file). You may share or redistribute
* a modified version of this file provided the following conditions are met:
*
* 1. The shared file or redistribution must retain the information set out
* above and this list of conditions.
* 2. Derivative's name (Derivative Inc.) or its trademarks may not be used
* to endorse or promote products derived from this file without specific
* prior written permission from Derivative.
*/

#include "CudaPOP.h"

#include <assert.h>
#include <cstdio>

// These functions are basic C function, which the DLL loader can find
// much easier than finding a C++ Class.
// The DLLEXPORT prefix is needed so the compile exports these functions from the .dll
// you are creating
extern "C"
{
DLLEXPORT
void
FillPOPPluginInfo(POP_PluginInfo *info)
{
	// This must always be set to this constant
	if (!info->setAPIVersion(POPCPlusPlusAPIVersion))
		return;

	// The opType is the unique name for this POP. It must start with a 
	// capital A-Z character, and all the following characters must lower case
	// or numbers (a-z, 0-9)
	info->customOPInfo.opType->setString("#__OP_TYPE__#");

	// The opLabel is the text that will show up in the OP Create Dialog
	info->customOPInfo.opLabel->setString("#__OP_LABEL__#");

	// Will be turned into a 3 letter icon on the nodes
	info->customOPInfo.opIcon->setString("#__OP_ICON__#");

	// Information about the author of this OP
	info->customOPInfo.authorName->setString("#__OP_AUTHOR__#");
	info->customOPInfo.authorEmail->setString("#__OP_EMAIL__#");

	// This POP works with 1 input connected
	info->customOPInfo.minInputs = 1;
	info->customOPInfo.maxInputs = 1;

	// Custom website URL that the Operator Help can point to
	info->customOPInfo.opHelpURL->setString("#__OP_HELPURL__#");
}

DLLEXPORT
POP_CPlusPlusBase*
CreatePOPInstance(const OP_NodeInfo* info, POP_Context *context)
{
	// Return a new instance of your class every time this is called.
	// It will be called once per POP that is using the .dll

	return new CudaPOP(info, context);
}

DLLEXPORT
void
DestroyPOPInstance(POP_CPlusPlusBase* instance, POP_Context *context)
{
	// Delete the instance here, this will be called when
	// Touch is shutting down, when the POP using that instance is deleted, or
	// if the POP loads a different DLL

	// We do some OpenGL teardown on destruction, so ask the POP_Context
	// to set up our OpenGL context

	delete (CudaPOP*)instance;
}

};


CudaPOP::CudaPOP(const OP_NodeInfo* info, POP_Context *context) :
	myNodeInfo(info), myExecuteCount(0),
	myError(nullptr),
	myStream(0),
	myContext(context)
{
	cudaStreamCreate(&myStream);
}

CudaPOP::~CudaPOP()
{
	if (myStream)
		cudaStreamDestroy(myStream);
}

void
CudaPOP::getGeneralInfo(POP_GeneralInfo* ginfo, const OP_Inputs *inputs, void* reserved)
{
}

// Forward declare the function here, it's defined in the kernel.cu file
extern cudaError_t shiftZPosition(const OP_SmartRef<POP_Buffer>& posBuffer, void* newDst, const OP_SmartRef<POP_Buffer>& pointInfo, const POP_MaxInfo& maxInfo, cudaStream_t stream);

static std::vector<std::pair<std::string, OP_SmartRef<POP_Buffer>>>
getAllAttributes(POP_AttributeClass c, const OP_POPInput* input, cudaStream_t stream)
{
	std::vector<std::pair<std::string, OP_SmartRef<POP_Buffer>>> res;
	for (uint32_t i = 0; i < input->getNumAttributes(c); i++)
	{
		const POP_Attribute* attr = input->getAttribute(c, i, nullptr);
		POP_GetBufferInfo info;
		info.location = POP_BufferLocation::CUDA;
		info.stream = stream;
		OP_SmartRef<POP_Buffer> buf = attr->getBuffer(info, nullptr);
		res.emplace_back(std::string(attr->info.name), buf);
	}
	return res;
}

void
CudaPOP::copyAllAttributes(POP_AttributeClass c, std::vector<std::pair<std::string, OP_SmartRef<POP_Buffer>>>& attrs,
							const POP_SetBufferInfo& sinfo, const OP_POPInput* input, POP_Output* output)
{
	for (auto& p : attrs)
	{
		const std::string& name = p.first;
		const POP_Attribute* attr = input->getAttribute(c, name.c_str(), nullptr);
		if (!attr || !p.second)
		{
			// This should never happen though, since the attrs array was built up from this input.
			continue;
		}
		Vector* b = (Vector*)p.second->getData(nullptr);
		output->setAttribute(&p.second, attr->info, sinfo, nullptr);
	}
}


void
CudaPOP::execute(POP_Output* output, const OP_Inputs* inputs, void* reserved)
{
	myError = nullptr;
	myExecuteCount++;

	if (inputs->getNumInputs() > 0)
	{
		const OP_POPInput* input = inputs->getInputPOP(0);

		if (!input)
			return;

		// We issue all the download operations first, then start processing them. This allows multiple
		// downloads to be occuring while we process the data
		std::vector<std::pair<std::string, OP_SmartRef<POP_Buffer>>> pointAttrs, vertAttrs, primAttrs;
		pointAttrs = getAllAttributes(POP_AttributeClass::Point, input, myStream);
		vertAttrs = getAllAttributes(POP_AttributeClass::Vertex, input, myStream);
		primAttrs = getAllAttributes(POP_AttributeClass::Primitive, input, myStream);

		POP_GetBufferInfo ginfo;
		ginfo.location = POP_BufferLocation::CPUOrCUDA;
		POP_InfoBuffers infoBufs;
		// You can individually get the Info buffers, or just get them all in one call.
		// If you do this you must use CPUOrCUDA or CPU, since some buffers are always
		// on the CPU.
#if 1
		input->getAllInfoBuffers(&infoBufs, ginfo, nullptr);
#else
		infoBufs.topoInfo = input->getTopologyInfo(ginfo, nullptr);
		infoBufs.pointInfo = input->getPointInfo(ginfo, nullptr);
		infoBufs.lineStripsInfo = input->getLineStripsInfo(ginfo, nullptr);
		infoBufs.lineStripsPrimIndices = input->getLineStripsPrimIndices(ginfo, nullptr);
		infoBufs.gridInfo = input->getGridInfo(ginfo, nullptr);
		info->getMaxInfo(&infoBufs.maxInfo, nullptr);
#endif
		POP_GetBufferInfo indexGInfo;
		indexGInfo.location = POP_BufferLocation::CUDA;
		indexGInfo.stream = myStream;
		OP_SmartRef<POP_Buffer> indexBuf = input->getIndexBuffer(nullptr)->getBuffer(indexGInfo, nullptr);

		OP_SmartRef<POP_Buffer> posBuf;
		for (auto& attr : pointAttrs)
		{
			if (attr.first == "P")
			{
				posBuf = attr.second;
				break;
			}
		}

		// If we find a position buffer, do a modification to it.
		if (posBuf)
		{
			void* newDst = nullptr;

// Uncomment this define to see how to create a buffer that you can fill in and send as the result instead
//#define COPY_EXAMPLE
#ifdef COPY_EXAMPLE
			POP_BufferInfo createInfo;
			createInfo = posBuf->info;
			OP_SmartRef<POP_Buffer> newPosBuf = myContext->createBuffer(createInfo, nullptr);
			newDst = newPosBuf->getData(nullptr);
#endif

			// Make sure all CUDA operations are done between these begin/end calls
			myContext->beginCUDAOperations(nullptr);

			// If newDst is nullptr, it will apply the operation to the posBuf data in-place.
			// Otherwise it will read from posBuf and write out ot newDst
			shiftZPosition(posBuf, newDst, infoBufs.pointInfo, infoBufs.maxInfo, myStream);

			myContext->endCUDAOperations(nullptr);

#ifdef COPY_EXAMPLE
			// Replace the buffer with the new one we created
			for (auto& attr : pointAttrs)
			{
				if (attr.second == posBuf)
				{
					attr.second = std::move(newPosBuf);
					break;
				}
			}
#endif
		}

		// How assign the outputs.
		// This example just passes the data though.
		// We can do edits to the buffers as we want though, and set them after editing them.

		POP_SetBufferInfo sinfo;
		sinfo.stream = myStream;
		copyAllAttributes(POP_AttributeClass::Point, pointAttrs, sinfo, input, output);
		copyAllAttributes(POP_AttributeClass::Vertex, vertAttrs, sinfo, input, output);
		copyAllAttributes(POP_AttributeClass::Primitive, primAttrs, sinfo, input, output);
		output->setIndexBuffer(&indexBuf, input->getIndexBuffer(nullptr)->info, sinfo, nullptr);
		output->setInfoBuffers(&infoBufs, sinfo, nullptr);
	}

}

int32_t
CudaPOP::getNumInfoCHOPChans(void* reserved)
{
	// We return the number of channel we want to output to any Info CHOP
	// connected to the POP. In this example we are just going to send one channel.
	return 1;
}

void
CudaPOP::getInfoCHOPChan(int32_t index,
						OP_InfoCHOPChan* chan,
						void* reserved)
{
	// This function will be called once for each channel we said we'd want to return
	// In this example it'll only be called once.

	if (index == 0)
	{
		chan->name->setString("executeCount");
		chan->value = (float)myExecuteCount;
	}
}

bool		
CudaPOP::getInfoDATSize(OP_InfoDATSize* infoSize, void* reserved)
{
	infoSize->rows = 1;
	infoSize->cols = 2;
	// Setting this to false means we'll be assigning values to the table
	// one row at a time. True means we'll do it one column at a time.
	infoSize->byColumn = false;
	return true;
}

void
CudaPOP::getInfoDATEntries(int32_t index,
										int32_t nEntries,
										OP_InfoDATEntries* entries,
										void* reserved)
{
	char tempBuffer[4096];

	if (index == 0)
	{
		// Set the value for the first column
#ifdef _WIN32
		strcpy_s(tempBuffer, "executeCount");
#else // macOS
		strlcpy(tempBuffer, "executeCount", sizeof(tempBuffer));
#endif
		entries->values[0]->setString(tempBuffer);

		// Set the value for the second column
#ifdef _WIN32
		sprintf_s(tempBuffer, "%d", myExecuteCount);
#else // macOS
		snprintf(tempBuffer, sizeof(tempBuffer), "%d", myExecuteCount);
#endif
		entries->values[1]->setString(tempBuffer);
	}
}

void
CudaPOP::getErrorString(OP_String *error, void* reserved)
{
	error->setString(myError);
}

void
CudaPOP::setupParameters(OP_ParameterManager* manager, void* reserved)
{
}

void
CudaPOP::pulsePressed(const char* name, void* reserved)
{
}
