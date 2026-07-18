package com.pethats;

import com.pethats.item.ModItems;
import com.pethats.loot.ModLootInjection;
import com.pethats.menu.ModMenus;
import com.pethats.pet.HatInteractionHandler;
import com.pethats.pet.ModAttachments;
import net.fabricmc.api.ModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public final class PetHats implements ModInitializer {

	public static final String MOD_ID = "pet_hats";
	public static final Logger LOGGER = LoggerFactory.getLogger("Pet Hats");

	@Override
	public void onInitialize() {
		ModItems.init();
		ModAttachments.init();
		ModMenus.init();
		HatInteractionHandler.init();
		ModLootInjection.init();
	}
}
